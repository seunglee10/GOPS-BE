from __future__ import annotations

import time
from typing import Any

from ..contracts import MarketEvent, stable_id, utc_now_iso
from ..intent_understanding import build_query_understanding, fallback_news_topic
from ..query_understanding import is_supported_company_symbol, supported_company_catalog_payload
from ..retrieval.snapshots import runtime_policy_from_env
from ..roles import AgentContext
from ..runtime import RuntimeRunContext
from .timing import empty_timing


def normalize_request_state(state: dict[str, Any]) -> dict[str, Any]:
    request = state["request"]
    timing = empty_timing()
    runtime_policy = runtime_policy_from_env()
    runtime_context = RuntimeRunContext(policy=runtime_policy, timing=timing)
    intent = str(request.get("intent") or request.get("prompt") or latest_message(request.get("messages")) or "analysis")
    analysis_mode = resolve_analysis_mode(request, intent)
    layout_context = request.get("layoutContext") if isinstance(request.get("layoutContext"), dict) else {}
    query_understanding, entity_resolution = build_query_understanding(
        intent,
        agent_ids=request.get("agentIds"),
        layout_context=layout_context,
        chart_context=request.get("chartContext"),
        request_symbol=request.get("symbol"),
        runtime_context=runtime_context,
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
    entity_resolution_payload = entity_resolution.to_dict()
    query_understanding.resolvedSymbol = symbol
    query_understanding.resolvedSymbolSource = symbol_source
    query_understanding.newsTopic = str(news_topic["label"]) if news_topic else None
    query_understanding.newsSymbols = list(news_topic["symbols"]) if news_topic else []
    query_understanding_payload = query_understanding.to_dict()
    query_understanding_payload["subjectValidation"] = dict(subject_validation)
    context = AgentContext(
        symbol=symbol,
        intent=intent,
        messages=[item for item in request.get("messages", []) if isinstance(item, dict)],
        chartContext=chart_context,
        layoutContext=layout_context,
        marketEvents=events,
        timing=timing,
        runtimeContext=runtime_context,
        newsSymbols=list(news_topic["symbols"]) if news_topic else [],
        newsTopic=str(news_topic["label"]) if news_topic else None,
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
        "entity_resolution": entity_resolution_payload,
        "query_understanding": query_understanding_payload,
        "subject_validation": subject_validation,
        "analysis_mode": analysis_mode,
        "route_mode": query_understanding.routeMode,
    }


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
