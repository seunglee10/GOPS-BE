import asyncio
import json
import re
import time
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.contracts.agents import AgentAnalysisRequest, AgentLayoutResolveRequest
from app.core.config import read_dotenv_value
from app.services.agent_alert_payloads import parse_pubsub_payload
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol, sp500_universe_symbols
from app.services.agent_gateway import get_agent_report, request_agent_analysis, request_agent_layout_resolution
from gops_agents.query_understanding import EntityResolution, KoreanEntityResolver
from gops_agents.query_understanding.korean_text import compact_text

router = APIRouter()
AGENT_ALERTS_CHANNEL = "agent.alerts"
AGENT_REPORTS_CHANNEL = "agent.reports"
CHART_SHORTCUT_MODE = "chartShortcut"

CHART_SHORTCUT_BLOCKING_KEYWORDS = (
    "뉴스",
    "기사",
    "보도",
    "헤드라인",
    "분석",
    "해석",
    "살펴",
    "거시",
    "금리",
    "관계",
    "온톨로지",
    "공급망",
    "경쟁사",
    "섹터",
    "급등",
    "급락",
    "이상",
    "변동",
    "원인",
    "왜",
    "차트",
    "그래프",
    "보여",
    "열어",
    "띄워",
    "바꿔",
    "변경",
    "조정",
    "news",
    "headline",
    "article",
    "analysis",
    "analyze",
    "inspect",
    "macro",
    "rate",
    "relationship",
    "ontology",
    "surge",
    "spike",
    "why",
    "chart",
    "graph",
    "show",
    "open",
    "change",
)


@router.post("/api/agents/analyze")
def analyze_agents(request: AgentAnalysisRequest, http_request: Request, response: Response) -> dict[str, Any]:
    result = request_agent_analysis(
        request.model_dump(),
        idempotency_key=http_request.headers.get("Idempotency-Key"),
        user_id=http_request.headers.get("X-GOPS-User-Id"),
    )
    response.status_code = int(result.pop("_status_code", 200))
    return result


@router.post("/api/agents/layout/resolve")
def resolve_agent_layout(request: AgentLayoutResolveRequest) -> dict[str, Any]:
    return request_agent_layout_resolution(request.model_dump())


@router.get("/api/agents/entities/resolve")
def resolve_agent_entity(
    q: str = Query(default="", max_length=128),
    mode: str = Query(default=CHART_SHORTCUT_MODE, max_length=32),
) -> dict[str, Any]:
    return resolve_agent_entity_for_chart_shortcut(q, mode=mode)


@router.get("/api/agents/reports/{analysis_id}")
def agent_report(analysis_id: str) -> dict[str, Any]:
    return get_agent_report(analysis_id)


@router.get("/api/agents/reports/{analysis_id}/stream")
async def agent_report_stream(analysis_id: str) -> StreamingResponse:
    return StreamingResponse(stream_agent_report_updates(analysis_id), media_type="text/event-stream")


@router.websocket("/ws/agent-alerts")
async def agent_alerts(
    websocket: WebSocket,
    symbol: str | None = Query(default=None, min_length=1, max_length=12),
) -> None:
    await websocket.accept()
    try:
        await websocket.send_json({"type": "AGENT_ALERTS_READY", "symbol": symbol})
        await stream_agent_alerts(websocket, symbol.upper() if symbol else None)
    except WebSocketDisconnect:
        return


async def stream_agent_alerts(websocket: WebSocket, symbol: str | None) -> None:
    import redis

    redis_url = read_dotenv_value("REDIS_URL") or "redis://localhost:6379/0"
    client = redis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    channels = [AGENT_ALERTS_CHANNEL]
    if symbol:
        channels.append(f"{AGENT_ALERTS_CHANNEL}:{symbol}")
    pubsub.subscribe(*channels)
    last_heartbeat = 0.0
    try:
        while True:
            message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
            if message and message.get("type") == "message":
                await websocket.send_json(parse_pubsub_payload(message.get("data")))
                continue
            now = time.monotonic()
            if now - last_heartbeat >= 25:
                last_heartbeat = now
                await websocket.send_json({"type": "HEARTBEAT", "source": "agent-alerts"})
    finally:
        pubsub.close()


async def stream_agent_report_updates(analysis_id: str):
    max_seconds = int(read_dotenv_value("AGENT_REPORT_STREAM_MAX_SECONDS") or "60")
    poll_seconds = float(read_dotenv_value("AGENT_REPORT_STREAM_POLL_SECONDS") or "1")
    deadline = time.monotonic() + max(1, max_seconds)
    last_status = None
    last_poll_at = 0.0
    terminal_statuses = {"completed", "failed", "deep_completed"}
    pubsub = report_update_pubsub(analysis_id)
    try:
        while time.monotonic() <= deadline:
            if pubsub is not None:
                message = await asyncio.to_thread(pubsub.get_message, timeout=0.25)
                if message and message.get("type") == "message":
                    report = parse_report_update_payload(message.get("data"))
                    if report.get("analysisId") == analysis_id:
                        status = str(report.get("status") or "unknown")
                        last_status = status
                        yield sse_event("report", report)
                        if status in terminal_statuses:
                            return
                        continue

            now = time.monotonic()
            if now - last_poll_at < max(0.1, poll_seconds):
                await asyncio.sleep(0.1)
                continue
            last_poll_at = now
            try:
                report = get_agent_report(analysis_id)
            except Exception as exc:
                yield sse_event("error", {"analysisId": analysis_id, "detail": str(exc)})
                return
            status = str(report.get("status") or "unknown")
            if status != last_status or status in terminal_statuses:
                last_status = status
                yield sse_event("report", report)
            if status in terminal_statuses:
                return
        yield sse_event("timeout", {"analysisId": analysis_id, "status": last_status or "unknown"})
    finally:
        if pubsub is not None:
            pubsub.close()


def sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def report_update_pubsub(analysis_id: str):
    if not bool_config("AGENT_REPORT_STREAM_REDIS_ENABLED", True):
        return None
    redis_url = read_dotenv_value("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis

        client = redis.from_url(redis_url, decode_responses=True)
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        channel_base = read_dotenv_value("AGENT_REPORT_UPDATES_CHANNEL") or AGENT_REPORTS_CHANNEL
        pubsub.subscribe(f"{channel_base}:{analysis_id}")
        return pubsub
    except Exception:
        return None


def parse_report_update_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        parsed = json.loads(str(payload))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_agent_entity_for_chart_shortcut(query: str, *, mode: str = CHART_SHORTCUT_MODE) -> dict[str, Any]:
    text = str(query or "").strip()
    if mode != CHART_SHORTCUT_MODE:
        return {
            "status": "unsupported",
            "chartShortcut": False,
            "reason": f"unsupported resolve mode: {mode}",
        }
    if not text:
        return {
            "status": "not_found",
            "chartShortcut": False,
            "reason": "empty query",
        }

    resolution = get_agent_entity_resolver().resolve(text)
    return agent_entity_resolution_payload(text, resolution)


def agent_entity_resolution_payload(query: str, resolution: EntityResolution) -> dict[str, Any]:
    status = resolution.status
    if status == "confirmed" and resolution.entity_type != "company":
        status = "unsupported"

    payload = {
        "status": status,
        "chartShortcut": is_chart_shortcut_entity_query(query, resolution),
        "symbol": resolution.symbol,
        "canonicalName": resolution.canonical_name,
        "matchedText": resolution.matched_text,
        "matchedAlias": resolution.matched_alias,
        "confidence": round(float(resolution.confidence or 0.0), 4),
        "entityType": resolution.entity_type,
        "reason": resolution.reason,
    }
    return {key: value for key, value in payload.items() if value is not None}


def is_chart_shortcut_entity_query(query: str, resolution: EntityResolution) -> bool:
    if resolution.status != "confirmed" or resolution.entity_type != "company" or not resolution.symbol:
        return False
    if not is_chart_symbol_supported(resolution.symbol):
        return False
    if contains_chart_shortcut_blocking_keyword(query):
        return False

    query_compact = compact_text(query)
    if not query_compact:
        return False
    allowed_values = [
        resolution.symbol,
        resolution.canonical_name,
        resolution.matched_text,
        resolution.matched_alias,
        *(candidate.canonical_name for candidate in resolution.candidates),
        *(candidate.matched_alias for candidate in resolution.candidates),
        *(candidate.matched_text for candidate in resolution.candidates),
    ]
    return any(query_compact == compact_text(value) for value in allowed_values if value)


def contains_chart_shortcut_blocking_keyword(query: str) -> bool:
    normalized = str(query or "").strip().lower()
    for keyword in CHART_SHORTCUT_BLOCKING_KEYWORDS:
        if re.fullmatch(r"[a-z]+", keyword):
            if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", normalized):
                return True
            continue
        if keyword in normalized:
            return True
    return False


@lru_cache(maxsize=2048)
def is_chart_symbol_supported(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return False
    try:
        normalized = normalize_market_symbol(normalized)
    except Exception:
        return False
    try:
        if normalized in set(sp500_universe_symbols()):
            return True
    except Exception:
        pass
    try:
        get_market_data_provider().symbol_detail(normalized)
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_agent_entity_resolver() -> KoreanEntityResolver:
    return KoreanEntityResolver()


def bool_config(name: str, default: bool) -> bool:
    value = read_dotenv_value(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
