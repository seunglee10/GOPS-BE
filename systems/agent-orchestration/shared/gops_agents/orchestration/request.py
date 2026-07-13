from __future__ import annotations

import re
import time
from typing import Any

from ..contracts import MarketEvent, stable_id, utc_now_iso
from ..intent_understanding import build_query_understanding, fallback_news_topic
from ..operations import build_agent_operation_ir, maybe_plan_operation_ir, normalize_operation_references
from ..query_understanding import is_supported_company_symbol, relationship_symbols_for_context, supported_company_catalog_payload
from ..query_understanding.seeds import COMPANY_SYMBOL_ALIASES
from ..retrieval.snapshots import runtime_policy_from_env
from ..roles import AgentContext
from ..runtime import RuntimeRunContext
from ..security import sanitize_value
from .timing import empty_timing


def normalize_request_state(state: dict[str, Any]) -> dict[str, Any]:
    request_result = sanitize_value(state["request"] if isinstance(state.get("request"), dict) else {})
    request = request_result.value if isinstance(request_result.value, dict) else {}
    input_safety_warnings = list(request_result.warnings)
    timing = empty_timing()
    runtime_policy = runtime_policy_from_env()
    runtime_context = RuntimeRunContext(policy=runtime_policy, timing=timing)
    intent = str(request.get("intent") or request.get("prompt") or latest_message(request.get("messages")) or "analysis")
    analysis_mode = resolve_analysis_mode(request, intent)
    layout_context = request.get("layoutContext") if isinstance(request.get("layoutContext"), dict) else {}
    ui_context = request.get("uiContext") if isinstance(request.get("uiContext"), dict) else {}
    chart_context_raw = request.get("chartContext") if isinstance(request.get("chartContext"), dict) else {}
    references = normalize_operation_references(request.get("references", []), ui_context, chart_context_raw)
    query_understanding, entity_resolution = build_query_understanding(
        intent,
        agent_ids=request.get("agentIds"),
        layout_context=layout_context,
        chart_context=request.get("chartContext"),
        request_symbol=request.get("symbol"),
        runtime_context=runtime_context,
        layout_command_preflight=bool(request.get("_layoutResolveOnly")),
        timing=timing,
    )
    explicit_symbol = (
        entity_resolution.symbol
        if entity_resolution.status == "confirmed" and entity_resolution.entity_type == "company"
        else None
    )
    news_topic = fallback_news_topic(intent, explicit_symbol, entity_resolution)
    symbol, symbol_source, symbol_from_query = resolve_subject_symbol(
        request=request,
        explicit_symbol=explicit_symbol,
        news_topic=news_topic,
    )
    subject_validation = validate_subject_symbol(
        symbol=symbol,
        symbol_source=symbol_source,
        route_mode=query_understanding.routeMode,
        entity_resolution=entity_resolution,
    )
    chart_context = sanitize_chart_context_for_symbol(request.get("chartContext"), symbol, symbol_from_query)
    events = [
        item if isinstance(item, MarketEvent) else MarketEvent.from_dict(item)
        for item in request.get("marketEvents", [])
        if isinstance(item, (dict, MarketEvent))
    ]
    relationship_symbols = relationship_symbols_for_context(intent, symbol)
    entity_resolution_payload = entity_resolution.to_dict()
    query_understanding.resolvedSymbol = symbol
    query_understanding.resolvedSymbolSource = symbol_source
    query_understanding.newsTopic = str(news_topic["label"]) if news_topic else None
    query_understanding.newsSymbols = list(news_topic["symbols"]) if news_topic else []
    query_understanding_payload = query_understanding.to_dict()
    query_understanding_payload = apply_chart_action_ui_task(request, query_understanding_payload, symbol)
    query_understanding_payload = apply_chart_ui_task_symbols(request, query_understanding_payload, symbol, intent)
    query_understanding_payload["subjectValidation"] = dict(subject_validation)
    operation_ir = build_agent_operation_ir(
        intent=intent,
        symbol=symbol,
        references=references,
        ui_context=ui_context,
        chart_context=chart_context,
    )
    operation_ir = maybe_plan_operation_ir(
        base_ir=operation_ir,
        intent=intent,
        symbol=symbol,
        references=references,
        ui_context=ui_context,
        chart_context=chart_context,
        runtime_context=runtime_context,
    ) or operation_ir
    query_understanding_payload = apply_operation_ir_to_query_understanding(query_understanding_payload, operation_ir)
    context = AgentContext(
        symbol=symbol,
        intent=intent,
        messages=[item for item in request.get("messages", []) if isinstance(item, dict)],
        chartContext=chart_context,
        layoutContext=layout_context,
        references=references,
        uiContext=ui_context,
        operationIR=operation_ir,
        marketEvents=events,
        timing=timing,
        runtimeContext=runtime_context,
        newsSymbols=list(news_topic["symbols"]) if news_topic else [],
        newsTopic=str(news_topic["label"]) if news_topic else None,
        relationshipSymbols=list(relationship_symbols),
        entityResolution=entity_resolution_payload,
        queryUnderstanding=query_understanding_payload,
        subjectValidation=subject_validation,
    )
    run_id = stable_id(
        "run",
        {
            "symbol": symbol,
            "intent": intent,
            "createdAt": request.get("createdAt") or utc_now_iso(),
        },
    )
    return {
        **state,
        "request": request,
        "run_id": run_id,
        "symbol": symbol,
        "intent": intent,
        "events": events,
        "context": context,
        "role_findings": [],
        "timing": timing,
        "runtime_context": runtime_context,
        "timing_started_at": time.perf_counter(),
        "news_topic": news_topic,
        "news_symbols": list(news_topic["symbols"]) if news_topic else [],
        "relationship_symbols": list(relationship_symbols),
        "entity_resolution": entity_resolution_payload,
        "query_understanding": query_understanding_payload,
        "operation_ir": operation_ir,
        "subject_validation": subject_validation,
        "input_safety_warnings": input_safety_warnings,
        "analysis_mode": analysis_mode,
        "route_mode": str(query_understanding_payload.get("routeMode") or query_understanding.routeMode),
    }


def apply_chart_action_ui_task(
    request: dict[str, Any],
    query_understanding: dict[str, Any],
    symbol: str,
) -> dict[str, Any]:
    chart_action = str(request.get("chartAction") or "").strip().lower()
    if chart_action != "add":
        return query_understanding
    target_symbol = normalize_symbol(request.get("chartTargetSymbol") or symbol)
    if not target_symbol or target_symbol == "UNKNOWN":
        return query_understanding
    placement_intent = str(request.get("chartPlacementIntent") or "").strip().lower() or None
    if placement_intent not in {"top", "bottom", "left", "right", "center"}:
        placement_intent = None
    task = {
        "action": "open",
        "confidence": 0.98,
        "source": "chart-shortcut",
        "reason": "Chart shortcut requested an additional chart panel.",
        "targetPanelType": "chart",
        "targetPanelId": None,
        "targetPanelTypes": ["chart"],
        "targetPanelIds": [],
        "layoutPreset": None,
        "sizeIntent": None,
        "positionIntent": placement_intent,
        "chartAction": "add",
        "symbol": target_symbol,
    }
    existing_tasks = query_understanding.get("uiTasks") if isinstance(query_understanding.get("uiTasks"), list) else []
    next_payload = dict(query_understanding)
    next_payload["routeMode"] = "ui_layout"
    next_payload["intentType"] = "ui-layout"
    next_payload["selectedRoles"] = []
    next_payload["contentTasks"] = []
    next_payload["uiTasks"] = [task, *existing_tasks]
    next_payload["resolvedSymbol"] = target_symbol
    next_payload["resolvedSymbolSource"] = "chart_shortcut"
    return next_payload


def apply_chart_ui_task_symbols(
    request: dict[str, Any],
    query_understanding: dict[str, Any],
    symbol: str,
    intent: str,
) -> dict[str, Any]:
    existing_tasks = query_understanding.get("uiTasks") if isinstance(query_understanding.get("uiTasks"), list) else []
    if not existing_tasks:
        return query_understanding
    chart_task_indexes = [
        index
        for index, task in enumerate(existing_tasks)
        if isinstance(task, dict) and task.get("targetPanelType") == "chart" and str(task.get("action") or "") in {"open", "focus"}
    ]
    if not chart_task_indexes:
        return query_understanding

    symbols = chart_symbols_from_intent(intent)
    request_symbol = normalize_symbol(request.get("chartTargetSymbol") or symbol)
    if request_symbol and request_symbol != "UNKNOWN" and request_symbol not in symbols:
        symbols.append(request_symbol)
    symbols = unique_symbols(symbols)
    add_signal = chart_add_signal(intent)

    next_tasks = list(existing_tasks)
    if len(symbols) >= 2:
        base_task = dict(existing_tasks[chart_task_indexes[0]])
        generated = [
            {
                **base_task,
                "action": "open",
                "targetPanelType": "chart",
                "targetPanelTypes": ["chart"],
                "targetPanelId": None,
                "targetPanelIds": [],
                "chartAction": "add",
                "symbol": symbol_value,
                "reason": f"Chart symbol '{symbol_value}' was resolved from a multi-symbol natural language request.",
            }
            for symbol_value in symbols[:2]
        ]
        other_tasks = [task for index, task in enumerate(existing_tasks) if index not in chart_task_indexes]
        next_payload = dict(query_understanding)
        next_payload["routeMode"] = "ui_layout"
        next_payload["intentType"] = "ui-layout"
        next_payload["selectedRoles"] = []
        next_payload["contentTasks"] = []
        next_payload["uiTasks"] = [*generated, *other_tasks]
        next_payload["resolvedSymbol"] = generated[0]["symbol"]
        next_payload["resolvedSymbolSource"] = "query_company"
        return next_payload

    target_symbol = symbols[0] if symbols else None
    if not target_symbol:
        return query_understanding

    for index in chart_task_indexes:
        task = next_tasks[index]
        if not isinstance(task, dict):
            continue
        updated = dict(task)
        updated["symbol"] = normalize_symbol(updated.get("symbol") or target_symbol)
        if add_signal and not updated.get("chartAction"):
            updated["chartAction"] = "add"
        next_tasks[index] = updated
    next_payload = dict(query_understanding)
    next_payload["uiTasks"] = next_tasks
    next_payload["resolvedSymbol"] = target_symbol
    if str(next_payload.get("resolvedSymbolSource") or "") in {"", "unresolved"}:
        next_payload["resolvedSymbolSource"] = "query_company"
    return next_payload


def chart_symbols_from_intent(intent: str) -> list[str]:
    normalized = str(intent or "").lower()
    compact = "".join(normalized.split())
    matches: list[tuple[int, str]] = []
    for alias, symbol in COMPANY_SYMBOL_ALIASES:
        alias_text = str(alias or "").lower()
        if not alias_text:
            continue
        use_compact = any("가" <= ch <= "힣" for ch in alias_text)
        haystack = compact if use_compact else normalized
        needle = "".join(alias_text.split()) if use_compact else alias_text
        index = haystack.find(needle)
        if index >= 0:
            matches.append((index, normalize_symbol(symbol)))
    for match in re.finditer(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b", str(intent or "").upper()):
        token = normalize_symbol(match.group(0))
        if is_supported_company_symbol(token):
            matches.append((match.start(), token))
    return unique_symbols([symbol for _, symbol in sorted(matches, key=lambda item: item[0])])


def chart_add_signal(intent: str) -> bool:
    compact = "".join(str(intent or "").lower().split())
    return any(token in compact for token in ("도", "추가", "추가로", "하나더", "같이", "함께", "두개", "2개", "나란히", "비교"))


def unique_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if not normalized or normalized == "UNKNOWN" or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def apply_operation_ir_to_query_understanding(
    query_understanding: dict[str, Any],
    operation_ir: dict[str, Any],
) -> dict[str, Any]:
    next_payload = dict(query_understanding)
    next_payload["operationIR"] = dict(operation_ir)
    roles = [
        role
        for role in operation_ir.get("suggestedRoles", [])
        if role in {"chart", "news", "macro", "ontology", "financial", "risk"}
    ]
    if not roles:
        return next_payload
    existing_roles = [
        role
        for role in next_payload.get("selectedRoles", [])
        if role in {"chart", "news", "macro", "ontology", "financial", "risk"}
    ]
    merged_roles = list(existing_roles)
    for role in roles:
        if role not in merged_roles:
            merged_roles.append(role)
    next_payload["selectedRoles"] = merged_roles
    confidence = float(operation_ir.get("confidence") or 0.0)
    if str(next_payload.get("routeMode") or "") == "clarify" and confidence >= 0.7:
        next_payload["routeMode"] = "analysis"
        next_payload["source"] = "operation-extractor"
        operations = operation_ir.get("operations") if isinstance(operation_ir.get("operations"), list) else []
        first_operation = operations[0] if operations and isinstance(operations[0], dict) else {}
        next_payload["intentType"] = str(first_operation.get("type") or next_payload.get("intentType") or "reference-analysis")
        next_payload["confidence"] = confidence
        next_payload["reason"] = "Explicit UI references resolved a short contextual analysis request."
    elif merged_roles != existing_roles:
        next_payload["confidence"] = max(float(next_payload.get("confidence") or 0.0), min(confidence, 0.92))
    return next_payload


def resolve_subject_symbol(*, request: dict[str, Any], explicit_symbol: str | None, news_topic: dict[str, Any] | None) -> tuple[str, str, bool]:
    if explicit_symbol:
        return normalize_symbol(explicit_symbol), "query_company", True
    if news_topic:
        return normalize_symbol(news_topic["label"]), "query_theme", True
    chart_context = request.get("chartContext")
    entity_fallback_symbol = read_entity_fallback_symbol_from_chart_context(chart_context)
    if entity_fallback_symbol:
        return normalize_symbol(entity_fallback_symbol), "chart_context_entity_fallback", False
    chart_symbol = read_chart_document_symbol_from_chart_context(chart_context)
    if chart_symbol:
        return normalize_symbol(chart_symbol), "chart_context_chart_document", False
    request_symbol = request.get("symbol")
    if request_symbol:
        return normalize_symbol(request_symbol), "request_symbol", False
    return "UNKNOWN", "unresolved", False


def read_entity_fallback_symbol_from_chart_context(context: Any) -> str | None:
    if not isinstance(context, dict):
        return None
    entity_fallback = context.get("entityFallback")
    if isinstance(entity_fallback, dict) and isinstance(entity_fallback.get("symbol"), str):
        return entity_fallback["symbol"]
    return None


def read_symbol_from_chart_context(context: Any) -> str | None:
    return read_entity_fallback_symbol_from_chart_context(context) or read_chart_document_symbol_from_chart_context(context)


def read_chart_document_symbol_from_chart_context(context: Any) -> str | None:
    if not isinstance(context, dict):
        return None
    chart_document = context.get("chartDocument")
    if isinstance(chart_document, dict) and isinstance(chart_document.get("symbol"), str):
        return chart_document["symbol"]
    return None


def sanitize_chart_context_for_symbol(context: Any, symbol: str, symbol_from_intent: bool) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    if not symbol_from_intent:
        return context
    chart_symbol = read_symbol_from_chart_context(context)
    if chart_symbol and normalize_symbol(chart_symbol) != symbol:
        return {}
    return context


def latest_message(messages: Any) -> str | None:
    if not isinstance(messages, list) or not messages:
        return None
    latest = messages[-1]
    if isinstance(latest, dict) and isinstance(latest.get("content"), str):
        return latest["content"]
    return None


def normalize_symbol(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return normalized or "UNKNOWN"


def resolve_analysis_mode(request: dict[str, Any], intent: str) -> str:
    raw_mode = str(request.get("analysisMode") or request.get("analysis_mode") or "").strip().lower()
    if raw_mode in {"multi_agent", "multi-agent", "multiagent"}:
        return "multi_agent"
    if raw_mode in {"auto", "single", "snapshot"}:
        return "auto"
    return "auto"


def validate_subject_symbol(
    *,
    symbol: str,
    symbol_source: str,
    route_mode: str,
    entity_resolution: Any,
) -> dict[str, Any]:
    if route_mode == "ui_layout":
        return {"status": "not_required", "reason": "layout-only request"}
    if symbol_source == "query_theme":
        return {"status": "supported", "subjectType": "theme", "catalog": supported_company_catalog_payload()}
    if not symbol or symbol == "UNKNOWN":
        return {
            "status": "unsupported",
            "subjectType": "company",
            "reason": "no supported company was resolved",
            "symbol": "UNKNOWN",
            "catalog": supported_company_catalog_payload(),
        }
    if is_supported_company_symbol(symbol):
        return {
            "status": "supported",
            "subjectType": "company",
            "symbol": symbol,
            "source": symbol_source,
            "catalog": supported_company_catalog_payload(),
        }
    raw_name = getattr(entity_resolution, "matched_text", "") or getattr(entity_resolution, "matched_alias", "") or symbol
    return {
        "status": "unsupported",
        "subjectType": "company",
        "reason": "company is not in the supported company catalog",
        "symbol": symbol,
        "rawName": raw_name,
        "source": symbol_source,
        "catalog": supported_company_catalog_payload(),
    }
