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
from app.services.agent_gateway import cancel_agent_analysis, get_agent_report, request_agent_analysis, request_agent_layout_resolution
from gops_agents.query_understanding import EntityResolution, KoreanEntityResolver, extract_relationship_symbols_from_intent
from gops_agents.query_understanding.korean_text import compact_text

router = APIRouter()
AGENT_ALERTS_CHANNEL = "agent.alerts"
AGENT_REPORTS_CHANNEL = "agent.reports"
CHART_SHORTCUT_MODE = "chartShortcut"

CHART_SHORTCUT_CONTENT_BLOCKING_KEYWORDS = (
    "뉴스",
    "기사",
    "보도",
    "헤드라인",
    "분석",
    "해석",
    "살펴",
    "재무",
    "재무제표",
    "재무상태표",
    "손익계산서",
    "현금흐름",
    "펀더멘탈",
    "실적",
    "매출",
    "영업이익",
    "순이익",
    "부채",
    "자산",
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
    "조정",
    "news",
    "headline",
    "article",
    "analysis",
    "analyze",
    "inspect",
    "financial",
    "finance",
    "fundamental",
    "fundamentals",
    "earnings",
    "revenue",
    "profit",
    "income",
    "balance sheet",
    "cash flow",
    "debt",
    "assets",
    "macro",
    "rate",
    "relationship",
    "ontology",
    "surge",
    "spike",
    "why",
)

CHART_SHORTCUT_OPEN_KEYWORDS = (
    "차트",
    "그래프",
    "캔들",
    "가격",
    "주가",
    "보여줘",
    "보여",
    "열어줘",
    "열어",
    "띄워줘",
    "띄워",
    "바꿔줘",
    "바꿔",
    "변경해줘",
    "변경",
    "chart",
    "graph",
    "candle",
    "price",
    "show",
    "open",
    "switch",
    "change",
)

CHART_SHORTCUT_ADD_KEYWORDS = (
    "추가",
    "추가해",
    "추가해줘",
    "같이",
    "함께",
    "동시에",
    "나란히",
    "비교",
    "도",
    "또",
    "add",
    "also",
    "too",
    "compare",
    "comparison",
    "side by side",
)

CHART_SHORTCUT_REPLACE_KEYWORDS = (
    "바꿔줘",
    "바꿔",
    "변경해줘",
    "변경",
    "전환",
    "대신",
    "switch",
    "change",
    "replace",
    "instead",
)

CHART_SHORTCUT_PLACEMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bottom": ("밑에", "아래", "하단", "bottom", "below", "down"),
    "top": ("위에", "위로", "상단", "top", "above", "up"),
    "left": ("왼쪽", "좌측", "left"),
    "right": ("오른쪽", "우측", "나란히", "옆에", "right", "side by side"),
    "center": ("가운데", "중앙", "center", "middle"),
}

CHART_SHORTCUT_FILLER_KEYWORDS = (
    "좀",
    "줘",
    "주세요",
    "랑",
    "이랑",
    "와",
    "과",
    "하고",
    "및",
    "에",
    "로",
    "으로",
    "please",
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


@router.post("/api/agents/reports/{analysis_id}/cancel")
def cancel_agent_report(analysis_id: str, http_request: Request) -> dict[str, Any]:
    return cancel_agent_analysis(analysis_id, user_id=http_request.headers.get("X-GOPS-User-Id"))


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
    terminal_statuses = {"completed", "failed", "deep_completed", "canceled"}
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

    extracted_symbols = chart_shortcut_symbols(query)
    single_chart_shortcut = is_chart_shortcut_entity_query(query, resolution)
    chart_shortcut = single_chart_shortcut or bool(extracted_symbols)
    symbols = chart_payload_symbols(resolution, extracted_symbols) if chart_shortcut else []
    if chart_shortcut and extracted_symbols:
        status = "confirmed"
    chart_action = chart_shortcut_action_for_symbols(query, resolution, symbols) if chart_shortcut else "none"
    payload = {
        "status": status,
        "chartShortcut": chart_shortcut,
        "chartAction": chart_action,
        "chartPlacementIntent": chart_shortcut_placement_intent(query) if chart_action == "add" else None,
        "symbol": symbols[0] if symbols else resolution.symbol,
        "symbols": symbols or None,
        "canonicalName": resolution.canonical_name,
        "matchedText": resolution.matched_text,
        "matchedAlias": resolution.matched_alias,
        "confidence": round(float(resolution.confidence or 0.0), 4),
        "entityType": resolution.entity_type,
        "reason": resolution.reason,
    }
    return {key: value for key, value in payload.items() if value is not None}


def chart_shortcut_symbols(query: str) -> list[str]:
    if contains_chart_shortcut_content_keyword(query):
        return []
    if not contains_compact_keyword(query, chart_shortcut_modifier_keywords()):
        return []
    symbols = [
        symbol
        for symbol in extract_relationship_symbols_from_intent(query, max_symbols=4)
        if symbol and is_chart_symbol_supported(symbol)
    ]
    return dedupe_symbols(symbols)


def chart_payload_symbols(resolution: EntityResolution, extracted_symbols: list[str]) -> list[str]:
    values = list(extracted_symbols)
    if not values and resolution.symbol and is_chart_symbol_supported(resolution.symbol):
        values.append(resolution.symbol)
    return dedupe_symbols(values)


def chart_shortcut_action_for_symbols(query: str, resolution: EntityResolution, symbols: list[str]) -> str:
    if len(symbols) > 1:
        return "add"
    if contains_compact_keyword(query, CHART_SHORTCUT_ADD_KEYWORDS):
        return "add"
    return chart_shortcut_action(query, resolution)


def dedupe_symbols(symbols: list[str]) -> list[str]:
    deduped = []
    for symbol in symbols:
        normalized = str(symbol or "").strip().upper()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def is_chart_shortcut_entity_query(query: str, resolution: EntityResolution) -> bool:
    if resolution.status != "confirmed" or resolution.entity_type != "company" or not resolution.symbol:
        return False
    if not is_chart_symbol_supported(resolution.symbol):
        return False
    if contains_chart_shortcut_content_keyword(query):
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
    allowed_compact_values = [compact_text(value) for value in allowed_values if value]
    if any(query_compact == value for value in allowed_compact_values if value):
        return True
    return is_chart_open_shortcut_query(query_compact, allowed_compact_values)


def is_chart_open_shortcut_query(query_compact: str, allowed_compact_values: list[str]) -> bool:
    remainder = query_compact
    matched_entity = False
    for value in sorted({value for value in allowed_compact_values if value}, key=len, reverse=True):
        if value in remainder:
            remainder = remainder.replace(value, "", 1)
            matched_entity = True
            break
    if not matched_entity or not remainder:
        return False

    open_remainder = remainder
    for keyword in compact_keywords(chart_shortcut_modifier_keywords()):
        open_remainder = open_remainder.replace(keyword, "")
    if open_remainder == remainder:
        return False
    for keyword in compact_keywords(CHART_SHORTCUT_FILLER_KEYWORDS):
        open_remainder = open_remainder.replace(keyword, "")
    return open_remainder == ""


def chart_shortcut_action(query: str, resolution: EntityResolution) -> str:
    query_compact = compact_text(query)
    remainder = query_compact
    for value in sorted(chart_resolution_compact_values(resolution), key=len, reverse=True):
        if value in remainder:
            remainder = remainder.replace(value, "", 1)
            break
    if contains_compact_keyword(remainder or query_compact, CHART_SHORTCUT_ADD_KEYWORDS):
        return "add"
    if contains_compact_keyword(remainder or query_compact, CHART_SHORTCUT_REPLACE_KEYWORDS):
        return "replace"
    return "replace"


def chart_shortcut_placement_intent(query: str) -> str | None:
    compacted = compact_text(query)
    for placement, keywords in CHART_SHORTCUT_PLACEMENT_KEYWORDS.items():
        if contains_compact_keyword(compacted, keywords):
            return placement
    return None


def chart_resolution_compact_values(resolution: EntityResolution) -> list[str]:
    values = [
        resolution.symbol,
        resolution.canonical_name,
        resolution.matched_text,
        resolution.matched_alias,
        *(candidate.canonical_name for candidate in resolution.candidates),
        *(candidate.matched_alias for candidate in resolution.candidates),
        *(candidate.matched_text for candidate in resolution.candidates),
    ]
    return [compact_text(value) for value in values if value]


def contains_compact_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    compacted = compact_text(text)
    return any(keyword and keyword in compacted for keyword in compact_keywords(keywords))


def compact_keywords(keywords: tuple[str, ...]) -> list[str]:
    return sorted({compact_text(keyword) for keyword in keywords if compact_text(keyword)}, key=len, reverse=True)


def chart_shortcut_modifier_keywords() -> tuple[str, ...]:
    placement_keywords = tuple(
        keyword
        for keywords in CHART_SHORTCUT_PLACEMENT_KEYWORDS.values()
        for keyword in keywords
    )
    return CHART_SHORTCUT_OPEN_KEYWORDS + CHART_SHORTCUT_ADD_KEYWORDS + CHART_SHORTCUT_REPLACE_KEYWORDS + placement_keywords


def contains_chart_shortcut_content_keyword(query: str) -> bool:
    normalized = str(query or "").strip().lower()
    for keyword in CHART_SHORTCUT_CONTENT_BLOCKING_KEYWORDS:
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
