import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from backend.app.contracts.chart import AgentChatRequest, chart_command_schema, filled_command_payload
from backend.app.core.config import read_dotenv_value
from backend.app.services.market_data import supported_dummy_symbols


def fallback_chart_proposal(context: dict[str, Any]) -> dict[str, Any]:
    chart_document = context.get("chartDocument") if isinstance(context.get("chartDocument"), dict) else {}
    visible_summary = context.get("visibleSummary") if isinstance(context.get("visibleSummary"), dict) else {}
    symbol = chart_document.get("symbol") if isinstance(chart_document.get("symbol"), str) else "AAPL"
    comparison_symbol = default_comparison_symbol(symbol)
    last_price = _read_float(visible_summary.get("lastPrice")) or _read_float(visible_summary.get("high")) or 100.0
    anchor = {
        "timestamp": "2026-06-25T15:30:00Z",
        "price": last_price,
        "paneId": "price",
        "symbol": symbol,
        "logicalIndex": 120,
    }

    return {
        "id": f"chart-proposal-{datetime.now(UTC).timestamp():.0f}",
        "title": "Agent 01 chart analysis preview",
        "summary": f"Preview a key price level and {comparison_symbol} comparison.",
        "rationale": "Drawing/comparison suggestions stay preview-first while ordinary viewport and MA commands can remain chart commands.",
        "createdByAgentId": "agent-01",
        "insights": [
            "Uses data-coordinate drawing anchors only.",
            "Keeps layout and trading state untouched.",
        ],
        "commands": [
            {
                "type": "chart.drawing.add",
                "payload": {
                    "drawingType": "horizontalLine",
                    "anchors": [anchor],
                    "style": {"color": "#2563eb", "lineWidth": 1.5},
                    "label": "Agent level",
                },
            },
            {
                "type": "chart.comparison.add",
                "payload": {
                    "comparison": {
                        "id": f"comparison-{comparison_symbol.lower()}-agent-preview",
                        "symbol": comparison_symbol,
                        "label": comparison_symbol,
                        "scaleMode": "percent",
                        "base": {"mode": "visibleRangeStart"},
                        "style": {"color": comparison_color(comparison_symbol), "lineWidth": 1.5},
                    },
                },
            },
        ],
    }


def _read_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_requested_symbol(text: str, current_symbol: str | None = None) -> str | None:
    upper = text.upper()
    current = current_symbol.upper() if isinstance(current_symbol, str) else None
    for symbol in supported_dummy_symbols():
        if symbol in upper and symbol != current:
            return symbol
    return None


def default_comparison_symbol(current_symbol: str | None) -> str:
    current = current_symbol.upper() if isinstance(current_symbol, str) else ""
    for symbol in ["SPY", "AAPL", "MSFT", "NVDA", "TSLA"]:
        if symbol in supported_dummy_symbols() and symbol != current:
            return symbol
    return supported_dummy_symbols()[0]


def comparison_color(symbol: str) -> str:
    return {
        "AAPL": "#2563eb",
        "MSFT": "#7c3aed",
        "NVDA": "#16a34a",
        "TSLA": "#dc2626",
        "SPY": "#0f766e",
    }.get(symbol, "#111111")


def fallback_agent_chat(request: AgentChatRequest) -> dict[str, Any]:
    latest = request.messages[-1].content if request.messages else ""
    upper = latest.upper()
    wants_comparison = any(word in latest.lower() for word in ["compare", "비교", "spy", "겹쳐", "오버레이"])
    commands: list[dict[str, Any]] = []
    reply_bits: list[str] = []

    if not wants_comparison:
        for symbol in supported_dummy_symbols():
            if symbol in upper:
                commands.append({
                    "type": "chart.symbol.set",
                    "payload": filled_command_payload(symbol=symbol),
                })
                reply_bits.append(f"symbol을 {symbol}로 전환")
                break

    for timeframe in ["10m", "5m", "1m"]:
        if timeframe in latest:
            commands.append({
                "type": "chart.timeframe.set",
                "payload": filled_command_payload(timeframe=timeframe),
            })
            reply_bits.append(f"timeframe을 {timeframe}로 변경")
            break

    if any(word in latest.lower() for word in ["zoom", "확대", "크게", "focus"]):
        commands.append({
            "type": "chart.viewport.set",
            "payload": filled_command_payload(visible_count=48, right_offset=0),
        })
        reply_bits.append("최근 구간을 확대")
    elif any(word in latest.lower() for word in ["wide", "전체", "축소"]):
        commands.append({
            "type": "chart.viewport.set",
            "payload": filled_command_payload(visible_count=120, right_offset=0),
        })
        reply_bits.append("더 넓은 구간 표시")

    if "MA20" in upper or "20" in latest:
        commands.append({
            "type": "chart.layer.visibility.set",
            "payload": filled_command_payload(layer="ma20", visible=True),
        })
        reply_bits.append("MA20 표시")

    chart_context = request.context.get("chartDocument") if isinstance(request.context.get("chartDocument"), dict) else {}
    visible_summary = request.context.get("visibleSummary") if isinstance(request.context.get("visibleSummary"), dict) else {}
    symbol = chart_context.get("symbol") if isinstance(chart_context.get("symbol"), str) else "AAPL"
    last_price = _read_float(visible_summary.get("lastPrice")) or _read_float(visible_summary.get("high")) or 100.0
    anchor = {
        "timestamp": "2026-06-25T15:30:00Z",
        "price": last_price,
        "paneId": "price",
        "symbol": symbol,
        "logicalIndex": 120,
    }

    if any(word in latest.lower() for word in ["line", "선", "지지", "저항", "level"]):
        commands.append({
            "type": "chart.drawing.add",
            "payload": {
                "drawingType": "horizontalLine",
                "anchors": [anchor],
                "style": {"color": "#2563eb", "lineWidth": 1.5},
                "label": "Agent level",
            },
        })
        reply_bits.append("수평 가격선 preview")

    if wants_comparison:
        comparison_symbol = normalize_requested_symbol(latest, symbol) or default_comparison_symbol(symbol)
        commands.append({
            "type": "chart.comparison.add",
            "payload": {
                "comparison": {
                    "id": f"comparison-{comparison_symbol.lower()}-chat-preview",
                    "symbol": comparison_symbol,
                    "label": comparison_symbol,
                    "scaleMode": "percent",
                    "base": {"mode": "visibleRangeStart"},
                    "style": {"color": comparison_color(comparison_symbol), "lineWidth": 1.5},
                },
            },
        })
        reply_bits.append(f"{comparison_symbol} comparison preview")

    reply = "요청을 차트 명령으로 해석했습니다."
    if reply_bits:
        reply = "요청을 반영해 " + ", ".join(reply_bits) + " 명령을 준비했습니다."

    return {
        "reply": reply,
        "title": "Agent 01 chart action",
        "summary": reply,
        "rationale": "Agent 01 maps natural-language chart requests to the same chart command runtime used by UI controls.",
        "createdByAgentId": "agent-01",
        "insights": ["Commands are chart-scoped.", "Layout and trading state are untouched."],
        "commands": commands,
    }


def openai_agent_chat(request: AgentChatRequest) -> dict[str, Any]:
    if not read_dotenv_value("OPENAI_API_KEY"):
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured.")

    model = read_dotenv_value("OPENAI_MODEL") or "gpt-5.2"
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
            "commands": chart_command_schema(supported_dummy_symbols(), 0),
        },
    }
    messages = [
        {
            "role": "system",
                "content": (
                    "You are GOPS Agent 01, the chart operator. Answer conversationally in the user's language. "
                    "When the user asks to change, draw, focus, compare, zoom, show, hide, or inspect the chart, "
                    "return chart commands using only the capability manifest and never mutate state directly. "
                    f"Comparison symbols must be one of: {', '.join(supported_dummy_symbols())}. "
                    "Do not include trading, account, order, or layout commands."
                ),
        },
        {
            "role": "user",
            "content": json.dumps({
                "agentIds": request.agentIds,
                "chartContext": request.context,
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
        raise HTTPException(status_code=502, detail=f"OpenAI proposal request failed with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="OpenAI proposal request could not be completed.") from exc

    text = extract_response_text(data)
    if not text:
        raise HTTPException(status_code=502, detail="OpenAI proposal response did not include JSON text.")
    return text


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
