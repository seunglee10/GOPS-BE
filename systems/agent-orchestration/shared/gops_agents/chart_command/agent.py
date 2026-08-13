from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

from market_data.serving.intervals import CHART_INTERVALS, normalize_chart_interval

from .schemas import chart_command_schema


ANALYSIS_KEYWORDS = ("analyze", "analysis", "inspect", "분석", "해석", "살펴")
ANALYSIS_TIMEFRAMES = CHART_INTERVALS


class ChartCommandError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ChatMessageLike(Protocol):
    role: str
    content: str

    def model_dump(self) -> dict[str, Any]:
        ...


class AgentChatRequestLike(Protocol):
    agentIds: list[str]
    messages: list[ChatMessageLike]
    context: dict[str, Any]


def _read_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ChartCommandAgent:
    def __init__(
        self,
        *,
        read_config: Callable[[str], str | None],
        configured_symbols: Callable[[], list[str]],
        response_requester: Callable[[dict[str, Any]], str] | None = None,
    ):
        self.read_config = read_config
        self.configured_symbols = configured_symbols
        self.response_requester = response_requester or (lambda payload: request_openai_response(payload, read_config=self.read_config))

    def chat(self, request: AgentChatRequestLike) -> dict[str, Any]:
        self._require_openai_api_key()
        model = self.read_config("OPENAI_MODEL") or "gpt-5.2"
        symbols = self.configured_symbols()
        analysis_request = is_chart_analysis_request(request)
        include_live_status = is_live_feed_status_request(request)
        chart_context = chart_context_for_agent_prompt(request.context, include_live_status=include_live_status)
        command_min_items = 1 if analysis_request else 0
        market_analysis_context = build_agent_market_analysis_context(chart_context, configured_symbols=self.configured_symbols)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["reply", "title", "summary", "rationale", "commands", "insights"],
            "properties": {
                "reply": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "rationale": {"type": "string"},
                "insights": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "commands": chart_command_schema(symbols, command_min_items),
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are GOPS Agent 01, the chart operator. Answer conversationally in the user's language. "
                    "When the user asks to change, draw, focus, compare, zoom, show, hide, or inspect the chart, "
                    "return chart commands using only the capability manifest and never mutate state directly. "
                    "For chart analysis requests, return at least one chart command. Prefer preview-first commands "
                    "that users can inspect on canvas: chart.drawing.add or chart.comparison.add. "
                    "If there is not enough evidence for a drawing/comparison, use a conservative "
                    "chart.viewport.set or chart.layer.visibility.set command and explain why. "
                    "When your answer mentions a price level, high, low, trend, comparison, moving average, or area to watch, "
                    "include a matching chart command using data-coordinate anchors from suggestedAnchors where possible. "
                    "Treat chartContext.dataStatus as historical chart-data readiness and chartContext.streamStatus as live-feed health only. "
                    "If chartContext.dataStatus.candleCount is greater than zero or chartContext.dataStatus.state is ready/partial, "
                    "do not say the chart cannot be analyzed just because streamStatus is stale or error. "
                    "Mention live-feed problems only when the user asks about live streaming or when no chart candles are available. "
                    "Do not invent market data, pixel coordinates, unsupported symbols, or unsupported commands. "
                    f"Comparison symbols must be one of: {', '.join(symbols)}. "
                    "Do not include trading, account, order, or layout commands."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "agentIds": request.agentIds,
                    "isChartAnalysisRequest": analysis_request,
                    "chartContext": chart_context,
                    "marketAnalysisContext": market_analysis_context,
                    "conversation": [message.model_dump() for message in request.messages[-8:]],
                }, ensure_ascii=True),
            },
        ]
        payload = {
            "model": model,
            "input": messages,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agent_chart_chat",
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        response = self.response_requester(payload)
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ChartCommandError(502, "OpenAI chat response was not valid JSON.") from exc
        parsed["createdByAgentId"] = "agent-01"
        return parsed

    def chart_proposal(self, context: dict[str, Any]) -> dict[str, Any]:
        self._require_openai_api_key()
        model = self.read_config("OPENAI_MODEL") or "gpt-5.2"
        symbols = self.configured_symbols()
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "summary", "rationale", "commands", "insights"],
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "rationale": {"type": "string"},
                "insights": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "commands": chart_command_schema(symbols, 1),
            },
        }
        payload = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are GOPS Agent 01. Return a chart-only proposal as JSON. "
                        f"Comparison symbols must be one of: {', '.join(symbols)}. "
                        "Never include layout, trading, account, order, or mixed-scope commands."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=True),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "chart_proposal",
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        text = self.response_requester(payload)
        try:
            proposal = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ChartCommandError(502, "OpenAI proposal response was not valid JSON.") from exc
        proposal["createdByAgentId"] = "agent-01"
        return proposal

    def _require_openai_api_key(self) -> None:
        if not self.read_config("OPENAI_API_KEY"):
            raise ChartCommandError(503, "OpenAI API key is not configured.")


def request_openai_response(payload: dict[str, Any], *, read_config: Callable[[str], str | None]) -> str:
    api_key = read_config("OPENAI_API_KEY")
    if not api_key:
        raise ChartCommandError(503, "OpenAI API key is not configured.")

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = extract_openai_error_detail(exc)
        message = f"OpenAI request failed with HTTP {exc.code}"
        if detail:
            message = f"{message}: {detail}"
        raise ChartCommandError(502, message) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ChartCommandError(502, "OpenAI proposal request could not be completed.") from exc

    text = extract_response_text(data)
    if not text:
        raise ChartCommandError(502, "OpenAI proposal response did not include JSON text.")
    return text


def is_chart_analysis_request(request: AgentChatRequestLike) -> bool:
    latest = request.messages[-1].content if request.messages else ""
    normalized = latest.lower()
    return any(keyword in normalized for keyword in ANALYSIS_KEYWORDS)


def is_live_feed_status_request(request: AgentChatRequestLike) -> bool:
    latest = request.messages[-1].content if request.messages else ""
    normalized = latest.lower()
    return any(keyword in normalized for keyword in ("stream", "websocket", "live feed", "실시간", "스트림", "웹소켓", "라이브", "연결 상태"))


def chart_context_for_agent_prompt(context: dict[str, Any], *, include_live_status: bool) -> dict[str, Any]:
    if include_live_status:
        return context
    sanitized = dict(context)
    sanitized.pop("streamStatus", None)
    return sanitized


def build_agent_market_analysis_context(context: dict[str, Any], *, configured_symbols: Callable[[], list[str]]) -> dict[str, Any]:
    chart_document = context.get("chartDocument") if isinstance(context.get("chartDocument"), dict) else {}
    visible_summary = context.get("visibleSummary") if isinstance(context.get("visibleSummary"), dict) else {}
    data_status = context.get("dataStatus") if isinstance(context.get("dataStatus"), dict) else {}
    raw_symbol = chart_document.get("symbol") if isinstance(chart_document.get("symbol"), str) else "UNKNOWN"
    symbols = configured_symbols()
    normalized_symbol = raw_symbol.upper()
    if symbols:
        symbol = normalized_symbol if normalized_symbol in symbols else symbols[0]
    else:
        symbol = normalized_symbol
    try:
        active_timeframe = normalize_chart_interval(chart_document.get("timeframe") if isinstance(chart_document.get("timeframe"), str) else "1m")
    except ValueError:
        active_timeframe = "1m"
    last_price = _read_float(visible_summary.get("lastPrice")) or _read_float(visible_summary.get("high"))
    candle_count = data_status.get("candleCount") if isinstance(data_status.get("candleCount"), int) else 0
    data_state = data_status.get("state") if isinstance(data_status.get("state"), str) else "unknown"
    stream_status = context.get("streamStatus") if isinstance(context.get("streamStatus"), str) else None
    data_readiness = {
        "state": data_state,
        "candleCount": candle_count,
        "hasUsableCandles": candle_count > 0 or data_state in {"ready", "partial"},
        "backfillStatus": data_status.get("backfillStatus") if isinstance(data_status.get("backfillStatus"), str) else None,
    }
    if stream_status:
        data_readiness["liveFeedStatus"] = stream_status

    return {
        "symbol": symbol,
        "dataReadiness": data_readiness,
        "activeView": {
            "timeframe": active_timeframe,
            "viewport": chart_document.get("viewport") if isinstance(chart_document.get("viewport"), dict) else {},
            "visibleSummary": visible_summary,
            "layers": chart_document.get("layers") if isinstance(chart_document.get("layers"), dict) else {},
        },
        "timeframes": {
            interval: {
                "interval": interval,
                "active": interval == active_timeframe,
                "visibleSummary": visible_summary if interval == active_timeframe else {},
            }
            for interval in ANALYSIS_TIMEFRAMES
        },
        "suggestedAnchors": suggested_analysis_anchors(symbol, visible_summary, last_price),
        "comparisonCandidates": [candidate for candidate in symbols if candidate != symbol],
    }


def suggested_analysis_anchors(symbol: str, visible_summary: dict[str, Any], last_price: float | None) -> list[dict[str, Any]]:
    anchors = []
    for role, field in (("currentPrice", "lastPrice"), ("visibleHigh", "high"), ("visibleLow", "low")):
        price = _read_float(visible_summary.get(field))
        if price is None and role == "currentPrice":
            price = last_price
        if price is None:
            continue
        anchors.append({
            "role": role,
            "timestamp": None,
            "price": round(price, 4),
            "paneId": "price",
            "symbol": symbol,
            "logicalIndex": None,
            "value": round(price, 4),
        })
    return anchors


def extract_openai_error_detail(error: urllib.error.HTTPError) -> str | None:
    try:
        body = error.read().decode("utf-8")
    except Exception:
        return None

    if not body.strip():
        return None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()[:600]

    error_payload = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error_payload, dict):
        message = error_payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:600]

    return body.strip()[:600]


def extract_response_text(data: dict[str, Any]) -> str | None:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text

    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                return content["text"]
    return None
