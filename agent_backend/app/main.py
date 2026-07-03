from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .analysis import analysis_tools, build_analysis_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART_DATA_BASE_URL = (os.getenv("CHART_DATA_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
DEFAULT_MODEL = "gpt-5.2"
ALLOWED_ACTIONS = {
    "setSymbol",
    "setInterval",
    "setTool",
    "toggleLayer",
    "setViewport",
    "addDrawing",
    "updateDrawing",
    "deleteDrawing",
    "selectDrawing",
    "clearDrawings",
}
INTERVALS = {"1m", "5m", "10m", "1D", "1W", "1M"}
DRAWING_TYPES = {"horizontalLine", "trendLine", "verticalMarker", "textLabel", "pointMarker", "arrow", "rangeBox", "measurement"}
LOGGER = logging.getLogger("gops.chart_agent")
DIRECT_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

app = FastAPI(title="GOPS Chart Agent Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://127.0.0.1:5176",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://localhost:5176",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/chart-agent/actions")
async def chart_agent_actions(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    panel = payload.get("panel") if isinstance(payload.get("panel"), dict) else {}
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    context = build_agent_context(prompt, panel, payload)
    provider = os.getenv("CHART_AGENT_PROVIDER", "").lower()
    api_key = read_env("OPENAI_API_KEY")
    if provider == "fake" or not api_key:
        return fake_agent_response(context)

    try:
        raw = call_openai(context, api_key)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    return normalize_agent_response(raw, context)


@app.get("/api/chart-analysis/tools")
async def get_chart_analysis_tools() -> dict[str, Any]:
    return {"tools": analysis_tools()}


@app.post("/api/chart-analysis/run")
async def run_chart_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(str(payload.get("symbol") or "GOPS-ALP"))
    interval = normalize_interval(str(payload.get("interval") or "1m"))
    requested_tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None
    candles = payload.get("candles") if isinstance(payload.get("candles"), list) else fetch_candles(symbol, interval, safe_int(payload.get("limit"), 240))
    snapshot = build_analysis_snapshot({"current": candles}, [str(tool) for tool in requested_tools] if requested_tools else None)
    return {"tools": analysis_tools(), "analysis": snapshot}


def build_agent_context(prompt: str, panel: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(str(panel.get("symbol") or "GOPS-ALP"))
    interval = normalize_interval(str(panel.get("interval") or "1m"))
    visible_count = safe_int(panel.get("visibleCount"), 120)
    right_offset = safe_int(panel.get("rightOffset"), 0)
    current = fetch_candles(symbol, interval, max(80, min(1_000, visible_count + right_offset + 80)))
    daily = fetch_candles(symbol, "1D", 160)
    weekly = fetch_candles(symbol, "1W", 80)
    symbols = fetch_symbols()
    candle_sets = {
        "current": current[-240:],
        "daily": daily[-120:],
        "weekly": weekly[-80:],
    }
    return {
        "prompt": prompt,
        "panel": {
            "symbol": symbol,
            "interval": interval,
            "visibleCount": visible_count,
            "rightOffset": right_offset,
            "layers": panel.get("layers") if isinstance(panel.get("layers"), dict) else {},
            "toolMode": panel.get("toolMode"),
            "trendLineExtension": panel.get("trendLineExtension"),
            "drawings": panel.get("drawings") if isinstance(panel.get("drawings"), list) else [],
        },
        "availableActions": [action for action in payload.get("availableActions", []) if action in ALLOWED_ACTIONS],
        "availableTools": analysis_tools(),
        "analysisSnapshot": build_analysis_snapshot(candle_sets),
        "futurePanCapacity": future_pan_capacity(visible_count),
        "symbols": symbols,
        "facts": {
            "current": summarize_candles(current),
            "daily": summarize_candles(daily),
            "weekly": summarize_candles(weekly),
        },
        "candles": candle_sets,
    }


def fetch_symbols() -> list[dict[str, Any]]:
    try:
        payload = get_json("/api/charts/symbols", {})
    except RuntimeError:
        return []
    symbols = payload.get("symbols")
    return symbols if isinstance(symbols, list) else []


def fetch_candles(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    try:
        payload = get_json(
            "/api/charts/candles",
            {
                "symbol": symbol,
                "interval": interval,
                "limit": str(limit),
                "session": "regular",
                "ma": "5,20,60",
            },
        )
    except RuntimeError:
        return []
    candles = payload.get("candles")
    return candles if isinstance(candles, list) else []


def get_json(path: str, params: dict[str, str]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{CHART_DATA_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with DIRECT_HTTP.open(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        LOGGER.warning("Chart data backend request failed for %s: %s", path, error)
        raise RuntimeError(f"chart data backend request failed: {path}") from error


def summarize_candles(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not candles:
        return {"count": 0}
    latest = candles[-1]
    max_volume = max(candles, key=lambda candle: safe_float(candle.get("volume"), 0))
    highest = max(candles, key=lambda candle: safe_float(candle.get("high"), 0))
    lowest = min(candles, key=lambda candle: safe_float(candle.get("low"), float("inf")))
    first_close = safe_float(candles[0].get("close"), 0)
    last_close = safe_float(latest.get("close"), first_close)
    change_percent = ((last_close - first_close) / first_close * 100) if first_close else 0
    return {
        "count": len(candles),
        "firstTimestamp": candles[0].get("timestamp"),
        "lastTimestamp": latest.get("timestamp"),
        "latestClose": latest.get("close"),
        "changePercent": round(change_percent, 2),
        "maxVolume": compact_candle(max_volume),
        "highest": compact_candle(highest),
        "lowest": compact_candle(lowest),
        "swing": swing_points(candles),
    }


def future_pan_capacity(visible_count: int) -> dict[str, int]:
    empty_slots = max(0, int((max(1, visible_count) * 2 + 2) // 3))
    return {
        "emptySlots": empty_slots,
        "minRightOffset": -empty_slots,
    }


def compact_candle(candle: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": candle.get("timestamp"),
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
        "volume": candle.get("volume"),
    }


def swing_points(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candles) < 2:
        return {}
    window = candles[-min(len(candles), 120):]
    low = min(window, key=lambda candle: safe_float(candle.get("low"), float("inf")))
    high = max(window, key=lambda candle: safe_float(candle.get("high"), 0))
    return {"low": compact_candle(low), "high": compact_candle(high)}


def fake_agent_response(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": "차트 에이전트 LLM provider가 비활성화되어 작업을 만들지 않았습니다.",
        "actions": [],
        "insights": ["분석 도구와 차트 액션 스키마는 사용할 수 있지만 LLM 호출이 비활성화되어 있습니다."],
    }


def call_openai(context: dict[str, Any], api_key: str) -> dict[str, Any]:
    model = read_env("OPENAI_MODEL") or DEFAULT_MODEL
    timeout = safe_int(read_env("OPENAI_TIMEOUT_SECONDS"), 20)
    request_payload = {
        "model": model,
        "instructions": (
            "You are a chart panel agent. Return only valid JSON matching the schema. "
            "You may only produce chart actions that a user can also perform in the frontend. "
            "Use availableTools and analysisSnapshot to decide which chart actions are useful. "
            "Do not invent symbols. Do not rely on hidden rules. "
            "You may use negative rightOffset values within futurePanCapacity when you need future whitespace for projections. "
            "Prefer one to five useful actions."
        ),
        "input": json.dumps(context, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "chart_agent_response",
                "strict": True,
                "schema": openai_response_schema(),
            }
        },
        "store": False,
        "max_output_tokens": 1_600,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI request failed: {error.code} {body[:240]}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("OpenAI request failed") from error

    text = extract_response_text(payload)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError("OpenAI response was not valid JSON") from error


def openai_response_schema() -> dict[str, Any]:
    null_string = ["string", "null"]
    anchor_schema = {
        "type": "object",
        "properties": {
            "timestamp": {"type": null_string},
            "logicalIndex": {"type": ["integer", "null"]},
            "price": {"type": ["number", "null"]},
            "paneId": {"type": ["string", "null"], "enum": ["price", "volume", None]},
            "symbol": {"type": null_string},
        },
        "required": ["timestamp", "logicalIndex", "price", "paneId", "symbol"],
        "additionalProperties": False,
    }
    style_schema = {
        "type": "object",
        "properties": {
            "color": {"type": null_string},
            "colorToken": {"type": null_string},
            "lineWidth": {"type": ["number", "null"]},
            "lineDash": {"type": ["array", "null"], "items": {"type": "number"}},
            "fillColor": {"type": null_string},
            "fillToken": {"type": null_string},
            "fillOpacity": {"type": ["number", "null"]},
            "textColor": {"type": null_string},
            "textToken": {"type": null_string},
            "fontSize": {"type": ["number", "null"]},
            "opacity": {"type": ["number", "null"]},
            "extension": {"type": ["string", "null"], "enum": ["segment", "ray", "line", None]},
        },
        "required": [
            "color",
            "colorToken",
            "lineWidth",
            "lineDash",
            "fillColor",
            "fillToken",
            "fillOpacity",
            "textColor",
            "textToken",
            "fontSize",
            "opacity",
            "extension",
        ],
        "additionalProperties": False,
    }
    drawing_schema = {
        "type": "object",
        "properties": {
            "id": {"type": null_string},
            "type": {"type": "string", "enum": sorted(DRAWING_TYPES)},
            "anchors": {"type": "array", "items": anchor_schema, "minItems": 1, "maxItems": 2},
            "style": style_schema,
            "label": {"type": null_string},
            "visible": {"type": ["boolean", "null"]},
            "createdBy": {"type": ["string", "null"], "enum": ["agent", "user", None]},
            "createdAt": {"type": null_string},
            "updatedAt": {"type": null_string},
        },
        "required": ["id", "type", "anchors", "style", "label", "visible", "createdBy", "createdAt", "updatedAt"],
        "additionalProperties": False,
    }
    patch_schema = {
        "type": "object",
        "properties": {
            "anchors": {"type": ["array", "null"], "items": anchor_schema},
            "style": {"anyOf": [style_schema, {"type": "null"}]},
            "label": {"type": null_string},
            "visible": {"type": ["boolean", "null"]},
        },
        "required": ["anchors", "style", "label", "visible"],
        "additionalProperties": False,
    }
    action_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
            "symbol": {"type": null_string},
            "interval": {"type": ["string", "null"], "enum": ["1m", "5m", "10m", "1D", "1W", "1M", None]},
            "toolMode": {"type": null_string},
            "layer": {"type": null_string},
            "visibleCount": {"type": ["integer", "null"]},
            "rightOffset": {"type": ["integer", "null"]},
            "drawingId": {"type": null_string},
            "drawing": {"anyOf": [drawing_schema, {"type": "null"}]},
            "patch": {"anyOf": [patch_schema, {"type": "null"}]},
        },
        "required": ["type", "symbol", "interval", "toolMode", "layer", "visibleCount", "rightOffset", "drawingId", "drawing", "patch"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "actions": {"type": "array", "items": action_schema, "maxItems": 8},
            "insights": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
        "required": ["message", "actions", "insights"],
        "additionalProperties": False,
    }


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise RuntimeError("OpenAI response did not include output text")


def normalize_agent_response(raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    actions = [normalize_action(action, context) for action in raw.get("actions", []) if isinstance(action, dict)]
    clean_actions = [action for action in actions if action][:8]
    message = raw.get("message") if isinstance(raw.get("message"), str) else "차트 에이전트가 작업을 만들었습니다."
    insights = [item for item in raw.get("insights", []) if isinstance(item, str)]
    return {"message": message, "actions": clean_actions, "insights": insights}


def normalize_action(action: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    action_type = action.get("type")
    panel = context["panel"]
    if action_type not in ALLOWED_ACTIONS:
        return None
    if action_type == "setSymbol":
        symbol = normalize_symbol(str(action.get("symbol") or panel["symbol"]))
        return {"type": "setSymbol", "symbol": symbol}
    if action_type == "setInterval":
        interval = normalize_interval(str(action.get("interval") or panel["interval"]))
        return {"type": "setInterval", "interval": interval}
    if action_type == "setTool":
        tool_mode = action.get("toolMode")
        return {"type": "setTool", "toolMode": tool_mode} if isinstance(tool_mode, str) else None
    if action_type == "toggleLayer":
        layer = action.get("layer")
        return {"type": "toggleLayer", "layer": layer} if layer in {"candles", "volume", "ma5", "ma20", "ma60"} else None
    if action_type == "setViewport":
        visible_count = max(20, safe_int(action.get("visibleCount"), safe_int(panel.get("visibleCount"), 120)))
        right_offset = safe_int(action.get("rightOffset"), safe_int(panel.get("rightOffset"), 0))
        min_right_offset = future_pan_capacity(visible_count)["minRightOffset"]
        return {
            "type": "setViewport",
            "visibleCount": visible_count,
            "rightOffset": max(min_right_offset, min(5_000, right_offset)),
        }
    if action_type == "addDrawing":
        drawing = action.get("drawing")
        clean_drawing = normalize_drawing(drawing, panel["symbol"]) if isinstance(drawing, dict) else None
        if clean_drawing:
            return {"type": "addDrawing", "drawing": clean_drawing}
        return None
    if action_type == "updateDrawing":
        drawing_id = action.get("drawingId")
        patch = action.get("patch")
        clean_patch = normalize_patch(patch)
        return {"type": "updateDrawing", "drawingId": drawing_id, "patch": clean_patch} if isinstance(drawing_id, str) and clean_patch else None
    if action_type == "deleteDrawing":
        drawing_id = action.get("drawingId")
        return {"type": "deleteDrawing", "drawingId": drawing_id} if isinstance(drawing_id, str) else None
    if action_type == "selectDrawing":
        drawing_id = action.get("drawingId")
        return {"type": "selectDrawing", "drawingId": drawing_id} if isinstance(drawing_id, str) else {"type": "selectDrawing"}
    return {"type": "clearDrawings"}


def normalize_drawing(drawing: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    drawing_type = drawing.get("type")
    anchors = drawing.get("anchors")
    if drawing_type not in DRAWING_TYPES or not isinstance(anchors, list) or not anchors:
        return None
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    clean_anchors = [normalize_anchor(anchor, symbol) for anchor in anchors if isinstance(anchor, dict)]
    if not clean_anchors:
        return None
    return {
        "id": drawing.get("id") if isinstance(drawing.get("id"), str) else f"agent-{uuid.uuid4()}",
        "type": drawing_type,
        "anchors": clean_anchors,
        "style": normalize_style(drawing.get("style")) or default_style(drawing_type),
        "label": drawing.get("label") if isinstance(drawing.get("label"), str) else None,
        "visible": bool(drawing.get("visible", True)),
        "createdBy": "agent",
        "createdAt": drawing.get("createdAt") if isinstance(drawing.get("createdAt"), str) else now,
        "updatedAt": now,
    }


def normalize_patch(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    patch: dict[str, Any] = {}
    anchors = value.get("anchors")
    if isinstance(anchors, list):
        clean_anchors = [normalize_anchor(anchor, "") for anchor in anchors if isinstance(anchor, dict)]
        if clean_anchors:
            patch["anchors"] = clean_anchors
    style = normalize_style(value.get("style"))
    if style:
        patch["style"] = style
    if isinstance(value.get("label"), str):
        patch["label"] = value["label"]
    if isinstance(value.get("visible"), bool):
        patch["visible"] = value["visible"]
    return patch or None


def normalize_anchor(anchor: dict[str, Any], symbol: str) -> dict[str, Any]:
    clean: dict[str, Any] = {"paneId": "price", "symbol": symbol}
    if isinstance(anchor.get("timestamp"), str):
        clean["timestamp"] = anchor["timestamp"]
    if isinstance(anchor.get("logicalIndex"), int):
        clean["logicalIndex"] = anchor["logicalIndex"]
    price = safe_float(anchor.get("price"), float("nan"))
    if price == price:
        clean["price"] = price
    return clean


def normalize_style(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    style: dict[str, Any] = {}
    for key in ("color", "colorToken", "fillColor", "fillToken", "textColor", "textToken", "extension"):
        if isinstance(value.get(key), str):
            style[key] = value[key]
    for key in ("lineWidth", "fontSize", "opacity", "fillOpacity"):
        if isinstance(value.get(key), (int, float)):
            style[key] = value[key]
    if isinstance(value.get("lineDash"), list):
        line_dash = [item for item in value["lineDash"] if isinstance(item, (int, float))]
        if line_dash:
            style["lineDash"] = line_dash
    return style or None


def chart_action(action_type: str, **kwargs: Any) -> dict[str, Any]:
    return {"type": action_type, **kwargs}


def vertical_marker(symbol: str, candle: dict[str, Any], label: str, suffix: str) -> dict[str, Any]:
    price = safe_float(candle.get("close"), safe_float(candle.get("high"), 0))
    return drawing("verticalMarker", [{"timestamp": candle.get("timestamp"), "price": price, "symbol": symbol}], label, suffix, {"colorToken": "down", "lineWidth": 1.6})


def horizontal_line(symbol: str, price: float, label: str) -> dict[str, Any]:
    return drawing("horizontalLine", [{"price": price, "symbol": symbol}], label, "agent-line", {"colorToken": "preview", "lineWidth": 1.5})


def trend_line(symbol: str, low: dict[str, Any], high: dict[str, Any], suffix: str) -> dict[str, Any]:
    anchors = [
        {"timestamp": low.get("timestamp"), "price": low.get("low"), "symbol": symbol},
        {"timestamp": high.get("timestamp"), "price": high.get("high"), "symbol": symbol},
    ]
    return drawing("trendLine", anchors, "추세 후보", suffix, {"colorToken": "drawing", "lineWidth": 1.6, "extension": "ray"})


def drawing(drawing_type: str, anchors: list[dict[str, Any]], label: str, suffix: str, style: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": f"{suffix}-{uuid.uuid4()}",
        "type": drawing_type,
        "anchors": anchors,
        "style": style,
        "label": label,
        "visible": True,
        "createdBy": "agent",
        "createdAt": now,
        "updatedAt": now,
    }


def default_style(drawing_type: str) -> dict[str, Any]:
    if drawing_type == "rangeBox":
        return {"colorToken": "preview", "fillToken": "preview", "fillOpacity": 0.12, "lineWidth": 1.4}
    if drawing_type == "measurement":
        return {"colorToken": "ma20", "textToken": "ma20", "lineWidth": 1.4}
    return {"colorToken": "drawing", "lineWidth": 1.5}


def normalize_symbol(symbol: str) -> str:
    value = symbol.upper()
    return value if value in {"GOPS-ALP", "GOPS-ION", "GOPS-NOVA"} else "GOPS-ALP"


def normalize_interval(interval: str) -> str:
    return interval if interval in INTERVALS else "1m"


def safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def read_env(key: str) -> str | None:
    if key in os.environ:
        return os.environ[key]
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None
