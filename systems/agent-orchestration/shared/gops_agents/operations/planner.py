from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable

from ..orchestration.routing import parse_openai_text_json
from .extractor import build_context_window_spec, roles_for_operations


OPERATION_TYPES = [
    "link_news_to_price_move",
    "explain_price_move",
    "explain_news",
    "summarize_news",
    "explain_relationship",
    "add_horizontal_line",
    "set_layer_visibility",
    "hide_other_indicator_layers",
]
ROLE_SOURCES = ["market", "news", "macro", "ontology", "financial"]
CHART_LAYERS = [
    "candles",
    "volume",
    "sma:5",
    "sma:20",
    "sma:60",
    "ema:20",
    "wma:20",
    "bollinger:20:2",
    "rsi:14",
    "stochastic:14:3:3",
    "macd:12:26:9",
    "volume-profile",
]


def maybe_plan_operation_ir(
    *,
    base_ir: dict[str, Any],
    intent: str,
    symbol: str,
    references: list[dict[str, Any]],
    ui_context: dict[str, Any],
    chart_context: dict[str, Any],
    runtime_context: Any | None = None,
    response_requester: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not operation_planner_enabled(response_requester=response_requester):
        return None
    if not needs_operation_planner(base_ir):
        return None
    if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
        if not runtime_context.acquire_llm("operation-planner"):
            return None
    try:
        raw = request_operation_plan(
            intent=intent,
            symbol=symbol,
            base_ir=base_ir,
            references=references,
            ui_context=ui_context,
            chart_context=chart_context,
            response_requester=response_requester,
        )
        planned_ir = operation_ir_from_planner_payload(
            raw,
            intent=intent,
            symbol=symbol,
            references=references,
            ui_context=ui_context,
            chart_context=chart_context,
        )
    except Exception:
        return None
    if not planned_ir.get("operations"):
        return None
    if float(planned_ir.get("confidence") or 0.0) < max(0.55, float(base_ir.get("confidence") or 0.0)):
        return None
    return planned_ir


def operation_planner_enabled(*, response_requester: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> bool:
    if response_requester is not None:
        return True
    return os.getenv("AGENT_OPERATION_PLANNER_PROVIDER") == "openai" and bool(os.getenv("OPENAI_API_KEY"))


def needs_operation_planner(base_ir: dict[str, Any]) -> bool:
    operations = base_ir.get("operations") if isinstance(base_ir.get("operations"), list) else []
    ambiguities = base_ir.get("ambiguities") if isinstance(base_ir.get("ambiguities"), list) else []
    confidence = float(base_ir.get("confidence") or 0.0)
    return not operations or bool(ambiguities) or confidence < 0.7


def request_operation_plan(
    *,
    intent: str,
    symbol: str,
    base_ir: dict[str, Any],
    references: list[dict[str, Any]],
    ui_context: dict[str, Any],
    chart_context: dict[str, Any],
    response_requester: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = operation_planner_payload(
        intent=intent,
        symbol=symbol,
        base_ir=base_ir,
        references=references,
        ui_context=ui_context,
        chart_context=chart_context,
    )
    data = response_requester(payload) if response_requester is not None else request_openai_json(payload)
    parsed = parse_openai_text_json(data)
    return parsed if isinstance(parsed, dict) else {}


def operation_planner_payload(
    *,
    intent: str,
    symbol: str,
    base_ir: dict[str, Any],
    references: list[dict[str, Any]],
    ui_context: dict[str, Any],
    chart_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": os.getenv("AGENT_OPERATION_PLANNER_MODEL", os.getenv("AGENT_ROUTER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2"))),
        "input": [
            {
                "role": "system",
                "content": (
                    "Parse one Korean stock app request into OperationIR candidates. "
                    "Return strict JSON only. Do not answer the user. Do not invent market data, candle prices, dates, symbols, or news facts. "
                    "Use the provided references and UI/chart context only as anchors. "
                    "For chart commands, output operation intent only; deterministic resolver will compute candles, prices, and anchors. "
                    "Use requiredSources only when an analysis operation needs evidence from that source."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "query": intent,
                        "symbol": symbol,
                        "baseOperationIR": base_ir,
                        "references": references,
                        "uiContext": ui_context,
                        "chartContext": compact_chart_context(chart_context),
                        "operationTypes": OPERATION_TYPES,
                        "chartLayers": CHART_LAYERS,
                        "requiredSources": ROLE_SOURCES,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "operation_ir_plan",
                "strict": True,
                "schema": operation_planner_schema(),
            }
        },
    }


def operation_planner_schema() -> dict[str, Any]:
    operation_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "kind",
            "type",
            "target",
            "visible",
            "priceSource",
            "requiredSources",
            "applyMode",
            "timeRange",
            "confidence",
        ],
        "properties": {
            "kind": {"type": "string", "enum": ["analysis", "chart"]},
            "type": {"type": "string", "enum": OPERATION_TYPES},
            "target": {"type": ["string", "null"], "enum": [*CHART_LAYERS, "price", None]},
            "visible": {"type": ["boolean", "null"]},
            "priceSource": {"type": ["string", "null"], "enum": ["open", "high", "low", "close", None]},
            "requiredSources": {"type": "array", "items": {"type": "string", "enum": ROLE_SOURCES}},
            "applyMode": {"type": ["string", "null"], "enum": ["persistent", "temporary", None]},
            "timeRange": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dateHints"],
                "properties": {
                    "dateHints": {"type": "array", "items": {"type": "string"}},
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["operations", "ambiguities", "confidence", "reason"],
        "properties": {
            "operations": {"type": "array", "items": operation_schema, "maxItems": 6},
            "ambiguities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["slot", "question"],
                    "properties": {
                        "slot": {"type": "string"},
                        "question": {"type": "string"},
                    },
                },
                "maxItems": 4,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    }


def operation_ir_from_planner_payload(
    payload: dict[str, Any],
    *,
    intent: str,
    symbol: str,
    references: list[dict[str, Any]],
    ui_context: dict[str, Any],
    chart_context: dict[str, Any],
) -> dict[str, Any]:
    operations = normalize_planned_operations(payload.get("operations"))
    roles = roles_for_operations(operations)
    dates = [
        value
        for operation in operations
        for value in operation.get("timeRange", {}).get("dateHints", [])
        if isinstance(value, str) and value
    ]
    layers = [
        str(operation.get("target"))
        for operation in operations
        if operation.get("kind") == "chart" and operation.get("target") in CHART_LAYERS
    ]
    price_sources = [
        str(operation.get("priceSource"))
        for operation in operations
        if operation.get("priceSource") in {"open", "high", "low", "close"}
    ]
    ambiguities = [
        {"slot": str(item.get("slot") or ""), "candidates": [], "question": str(item.get("question") or "")}
        for item in payload.get("ambiguities", [])
        if isinstance(item, dict) and item.get("question")
    ]
    confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    if ambiguities:
        confidence = min(confidence, 0.69)
    return {
        "version": 1,
        "source": "operation-planner-openai",
        "operations": operations,
        "entities": {
            "symbols": [symbol] if symbol and symbol != "UNKNOWN" else [],
            "dates": dedupe(dates),
            "layers": dedupe(layers),
            "priceSources": dedupe(price_sources),
        },
        "references": references,
        "contextWindow": build_context_window_spec(references, dedupe(dates), roles, chart_context or {}),
        "ambiguities": ambiguities,
        "suggestedRoles": roles,
        "confidence": confidence,
        "plannerReason": str(payload.get("reason") or f"Structured operation planner parsed: {intent}"),
    }


def normalize_planned_operations(value: Any) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        op_type = str(item.get("type") or "")
        kind = str(item.get("kind") or "")
        if op_type not in OPERATION_TYPES or kind not in {"analysis", "chart"}:
            continue
        if kind == "chart" and not op_type in {"add_horizontal_line", "set_layer_visibility", "hide_other_indicator_layers"}:
            continue
        if kind == "analysis" and op_type in {"add_horizontal_line", "set_layer_visibility", "hide_other_indicator_layers"}:
            continue
        operation: dict[str, Any] = {
            "kind": kind,
            "type": op_type,
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
        }
        if kind == "analysis":
            operation["requiredSources"] = normalize_required_sources(item.get("requiredSources"), op_type)
            operation["anchorReferences"] = []
        else:
            operation["target"] = item.get("target")
            operation["applyMode"] = item.get("applyMode") or "persistent"
            if item.get("visible") is not None:
                operation["visible"] = bool(item.get("visible"))
            if item.get("priceSource") in {"open", "high", "low", "close"}:
                operation["priceSource"] = item.get("priceSource")
            time_range = item.get("timeRange") if isinstance(item.get("timeRange"), dict) else {}
            operation["timeRange"] = {"dateHints": [value for value in time_range.get("dateHints", []) if isinstance(value, str)]}
        operations.append(operation)
    return operations


def normalize_required_sources(value: Any, op_type: str) -> list[str]:
    values = [item for item in value if item in ROLE_SOURCES] if isinstance(value, list) else []
    if values:
        return dedupe(values)
    defaults = {
        "link_news_to_price_move": ["market", "news", "ontology"],
        "explain_price_move": ["market", "news", "macro"],
        "explain_news": ["news", "market", "ontology"],
        "summarize_news": ["news"],
        "explain_relationship": ["ontology"],
    }
    return list(defaults.get(op_type, []))


def compact_chart_context(chart_context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(chart_context, dict):
        return {}
    compact = dict(chart_context)
    candles = compact.get("candles")
    if isinstance(candles, list) and len(candles) > 30:
        compact["candles"] = [*candles[:5], {"omittedMiddleCandles": len(candles) - 10}, *candles[-5:]]
    return compact


def request_openai_json(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {}
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_OPERATION_PLANNER_TIMEOUT_SECONDS", "4"))) as response:
        return json.loads(response.read().decode("utf-8"))


def dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped
