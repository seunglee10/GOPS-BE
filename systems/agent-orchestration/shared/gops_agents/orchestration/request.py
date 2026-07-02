from __future__ import annotations

import time
from typing import Any

from ..contracts import MarketEvent, stable_id, utc_now_iso
from ..query_understanding import extract_news_topic_from_intent, resolve_entity
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
    entity_started_at = time.perf_counter()
    entity_resolution = resolve_entity(intent, chart_context=request.get("chartContext"))
    timing["entityResolveMs"] = (time.perf_counter() - entity_started_at) * 1000
    explicit_symbol = (
        entity_resolution.symbol
        if entity_resolution.status == "confirmed" and entity_resolution.entity_type == "company"
        else None
    )
    news_topic = topic_from_entity_resolution(entity_resolution)
    if explicit_symbol:
        news_topic = None
    if news_topic is None and not explicit_symbol:
        news_topic = extract_news_topic_from_intent(intent)
    symbol = normalize_symbol(
        explicit_symbol
        or (news_topic["label"] if news_topic else None)
        or request.get("symbol")
        or read_symbol_from_chart_context(request.get("chartContext"))
        or "AAPL"
    )
    chart_context = sanitize_chart_context_for_symbol(request.get("chartContext"), symbol, bool(explicit_symbol or news_topic))
    events = [
        item if isinstance(item, MarketEvent) else MarketEvent.from_dict(item)
        for item in request.get("marketEvents", [])
        if isinstance(item, (dict, MarketEvent))
    ]
    entity_resolution_payload = entity_resolution.to_dict()
    context = AgentContext(
        symbol=symbol,
        intent=intent,
        messages=[item for item in request.get("messages", []) if isinstance(item, dict)],
        chartContext=chart_context,
        layoutContext=request.get("layoutContext") if isinstance(request.get("layoutContext"), dict) else {},
        marketEvents=events,
        timing=timing,
        runtimeContext=runtime_context,
        newsSymbols=list(news_topic["symbols"]) if news_topic else [],
        newsTopic=str(news_topic["label"]) if news_topic else None,
        entityResolution=entity_resolution_payload,
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
    }


def read_symbol_from_chart_context(context: Any) -> str | None:
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
    return normalized or "AAPL"


def topic_from_entity_resolution(entity_resolution: Any) -> dict[str, Any] | None:
    if getattr(entity_resolution, "status", None) != "confirmed":
        return None
    if getattr(entity_resolution, "entity_type", None) != "theme":
        return None
    symbols = [str(symbol).upper() for symbol in getattr(entity_resolution, "theme_symbols", ()) if str(symbol or "").strip()]
    if not symbols:
        return None
    return {
        "label": getattr(entity_resolution, "theme_name", None) or getattr(entity_resolution, "canonical_name", None) or "theme",
        "symbols": tuple(symbols),
        "source": getattr(entity_resolution, "catalog_source", None),
        "entityId": getattr(entity_resolution, "entity_id", None),
    }
