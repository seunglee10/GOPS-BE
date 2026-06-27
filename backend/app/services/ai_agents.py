import json
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

from backend.app.contracts.chart import AgentChatRequest, chart_command_schema
from backend.app.core.config import read_dotenv_value
from backend.app.services.market_data import build_dummy_candles, normalize_dummy_symbol, supported_dummy_symbols

ANALYSIS_KEYWORDS = ("analyze", "analysis", "inspect", "분석", "해석", "살펴")
ANALYSIS_TIMEFRAMES = ("1m", "5m", "10m")


def openai_agent_chat(request: AgentChatRequest) -> dict[str, Any]:
    if not read_dotenv_value("OPENAI_API_KEY"):
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured.")

    model = read_dotenv_value("OPENAI_MODEL") or "gpt-5.2"
    analysis_request = is_chart_analysis_request(request)
    command_min_items = 1 if analysis_request else 0
    market_analysis_context = build_agent_market_analysis_context(request.context)
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
            "commands": chart_command_schema(supported_dummy_symbols(), command_min_items),
        },
    }
    messages = [
        {
            "role": "system",
                "content": (
                    "You are GOPS Agent 01, the chart operator. Answer conversationally in the user's language. "
                    "When the user asks to change, draw, focus, compare, zoom, show, hide, or inspect the chart, "
                    "return chart commands using only the capability manifest and never mutate state directly. "
                    "The currently visible chart is only the starting UI view; for analysis requests, use the supplied "
                    "multi-timeframe marketAnalysisContext across 1m, 5m, and 10m instead of only the active timeframe. "
                    "For chart analysis requests, return at least one chart command. Prefer preview-first commands "
                    "that users can inspect on canvas: chart.drawing.add, chart.comparison.add, or chart.measurement.add. "
                    "If there is not enough evidence for a drawing/comparison/measurement, use a conservative "
                    "chart.viewport.set or chart.layer.visibility.set command and explain why. "
                    "When your answer mentions a price level, high, low, trend, comparison, moving average, or area to watch, "
                    "include a matching chart command using data-coordinate anchors from suggestedAnchors where possible. "
                    "Do not invent market data, pixel coordinates, unsupported symbols, or unsupported commands. "
                    f"Comparison symbols must be one of: {', '.join(supported_dummy_symbols())}. "
                    "Do not include trading, account, order, or layout commands."
                ),
        },
        {
            "role": "user",
            "content": json.dumps({
                "agentIds": request.agentIds,
                "isChartAnalysisRequest": analysis_request,
                "chartContext": request.context,
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
    response = request_openai_response(payload)
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="OpenAI chat response was not valid JSON.") from exc
    parsed["createdByAgentId"] = "agent-01"
    return parsed


def is_chart_analysis_request(request: AgentChatRequest) -> bool:
    latest = request.messages[-1].content if request.messages else ""
    normalized = latest.lower()
    return any(keyword in normalized for keyword in ANALYSIS_KEYWORDS)


def build_agent_market_analysis_context(context: dict[str, Any]) -> dict[str, Any]:
    chart_document = context.get("chartDocument") if isinstance(context.get("chartDocument"), dict) else {}
    visible_summary = context.get("visibleSummary") if isinstance(context.get("visibleSummary"), dict) else {}
    raw_symbol = chart_document.get("symbol") if isinstance(chart_document.get("symbol"), str) else "AAPL"
    symbol = normalize_dummy_symbol(raw_symbol)
    active_timeframe = chart_document.get("timeframe") if isinstance(chart_document.get("timeframe"), str) else "1m"
    timeframe_summaries = {
        interval: build_timeframe_analysis_summary(symbol, interval)
        for interval in ANALYSIS_TIMEFRAMES
    }

    return {
        "symbol": symbol,
        "activeView": {
            "timeframe": active_timeframe,
            "viewport": chart_document.get("viewport") if isinstance(chart_document.get("viewport"), dict) else {},
            "visibleSummary": visible_summary,
            "layers": chart_document.get("layers") if isinstance(chart_document.get("layers"), dict) else {},
        },
        "timeframes": timeframe_summaries,
        "suggestedAnchors": [
            anchor
            for summary in timeframe_summaries.values()
            for anchor in summary["suggestedAnchors"]
        ],
        "comparisonCandidates": [
            candidate
            for candidate in supported_dummy_symbols()
            if candidate != symbol
        ],
    }


def build_timeframe_analysis_summary(symbol: str, interval: str) -> dict[str, Any]:
    candles = build_dummy_candles(symbol, interval, 180)
    first = candles[0]
    last = candles[-1]
    recent_window = candles[-30:]
    previous_volume_window = candles[-60:-30]
    range_high = max(candles, key=lambda candle: float(candle["high"]))
    range_low = min(candles, key=lambda candle: float(candle["low"]))
    recent_high = max(recent_window, key=lambda candle: float(candle["high"]))
    recent_low = min(recent_window, key=lambda candle: float(candle["low"]))
    change_percent = ((float(last["close"]) - float(first["open"])) / max(0.0001, float(first["open"]))) * 100

    return {
        "interval": interval,
        "lastPrice": round(float(last["close"]), 4),
        "changePercent": round(change_percent, 2),
        "rangeHigh": candle_level(range_high, "high", symbol),
        "rangeLow": candle_level(range_low, "low", symbol),
        "recentSwingHigh": candle_level(recent_high, "high", symbol),
        "recentSwingLow": candle_level(recent_low, "low", symbol),
        "volumeDirection": volume_direction(previous_volume_window, recent_window),
        "maRelation": ma_relation(last),
        "suggestedAnchors": [
            candle_anchor(last, "close", symbol, "currentPrice"),
            candle_anchor(recent_high, "high", symbol, "recentSwingHigh"),
            candle_anchor(recent_low, "low", symbol, "recentSwingLow"),
        ],
    }


def candle_level(candle: dict[str, Any], field: str, symbol: str) -> dict[str, Any]:
    return {
        "timestamp": candle["timestamp"],
        "price": round(float(candle[field]), 4),
        "symbol": symbol,
    }


def candle_anchor(candle: dict[str, Any], field: str, symbol: str, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "timestamp": candle["timestamp"],
        "price": round(float(candle[field]), 4),
        "paneId": "price",
        "symbol": symbol,
        "logicalIndex": None,
        "value": round(float(candle[field]), 4),
    }


def volume_direction(previous_window: list[dict[str, Any]], recent_window: list[dict[str, Any]]) -> str:
    previous_volume = sum(int(candle["volume"]) for candle in previous_window)
    recent_volume = sum(int(candle["volume"]) for candle in recent_window)
    if previous_volume <= 0:
        return "unknown"

    ratio = recent_volume / previous_volume
    if ratio > 1.08:
        return "rising"
    if ratio < 0.92:
        return "falling"
    return "flat"


def ma_relation(candle: dict[str, Any]) -> dict[str, Any]:
    close = float(candle["close"])
    relation: dict[str, Any] = {}
    for key in ("ma5", "ma20", "ma60"):
        value = candle.get(key)
        if not isinstance(value, int | float):
            relation[key] = {"state": "unavailable", "value": None}
            continue
        relation[key] = {
            "state": "above" if close >= float(value) else "below",
            "value": round(float(value), 4),
            "distancePercent": round(((close - float(value)) / max(0.0001, float(value))) * 100, 2),
        }
    return relation


def openai_chart_proposal(context: dict[str, Any]) -> dict[str, Any]:
    if not read_dotenv_value("OPENAI_API_KEY"):
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured.")

    model = read_dotenv_value("OPENAI_MODEL") or "gpt-5.2"
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "summary", "rationale", "commands", "insights"],
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
            "insights": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "commands": chart_command_schema(supported_dummy_symbols(), 1),
        },
    }
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are GOPS Agent 01. Return a chart-only proposal as JSON. "
                    f"Comparison symbols must be one of: {', '.join(supported_dummy_symbols())}. "
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
    text = request_openai_response(payload)
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="OpenAI proposal response was not valid JSON.") from exc

    proposal["createdByAgentId"] = "agent-01"
    return proposal


def request_openai_response(payload: dict[str, Any]) -> str:
    api_key = read_dotenv_value("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured.")

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
        raise HTTPException(status_code=502, detail=message) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="OpenAI proposal request could not be completed.") from exc

    text = extract_response_text(data)
    if not text:
        raise HTTPException(status_code=502, detail="OpenAI proposal response did not include JSON text.")
    return text


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
