from __future__ import annotations

import json
import re
from typing import Any

from ..contracts import IntentRoute
from ..runtime.analysis_cache import CachedAgentAnalysis, analysis_cache_key


def analysis_cache_key_for_state(state: dict[str, Any]) -> str | None:
    if not is_analysis_cacheable_state(state):
        return None
    route = state["route"]
    selected_roles = [role for role in route.selectedRoles if role in {"chart", "news", "macro", "ontology", "financial"}]
    payload = {
        "symbol": state["symbol"],
        "intent": canonical_analysis_intent(state["intent"], route, selected_roles),
        "routerMode": str(state["request"].get("routerMode") or "hybrid"),
        "route": {
            "source": route.source,
            "intentType": route.intentType,
            "selectedRoles": selected_roles,
        },
        "routeMode": analysis_cache_route_mode(state),
        "events": [event.eventId for event in state.get("events", [])],
        "chartContext": chart_context_cache_payload(state["context"].chartContext, selected_roles),
        "references": references_cache_payload(getattr(state["context"], "references", [])),
        "operationIR": operation_ir_cache_payload(getattr(state["context"], "operationIR", {})),
        "newsSymbols": list(state.get("news_symbols", [])),
    }
    return analysis_cache_key(symbol=state["symbol"], payload=payload)


def is_analysis_cacheable_state(state: dict[str, Any]) -> bool:
    if state.get("analysis_mode") == "multi_agent":
        return False
    if is_ui_layout_state(state):
        return False
    route = state.get("route")
    if not isinstance(route, IntentRoute) or not route.selectedRoles:
        return False
    router_mode = str(state["request"].get("routerMode") or "hybrid").strip().lower()
    if router_mode in {"strict-llm", "llm"} or route.source == "strict-llm":
        return False
    intent = str(state.get("intent") or "")
    if has_order_or_account_terms(intent) or is_live_status_intent(intent):
        return False
    return True


def is_news_only_state(state: dict[str, Any]) -> bool:
    route = state.get("route")
    return isinstance(route, IntentRoute) and route.intentType == "news" and list(state.get("selected_roles", [])) == ["news"]


def analysis_cache_route_mode(state: dict[str, Any]) -> str:
    route_mode = str(state.get("route_mode") or "analysis")
    return "analysis" if route_mode == "hybrid" else route_mode


def is_ui_layout_state(state: dict[str, Any]) -> bool:
    if str(state.get("route_mode") or "").strip() == "ui_layout":
        return True
    ui_intent = state.get("ui_intent")
    route = state.get("route")
    return bool(ui_intent and getattr(ui_intent, "isUiIntent", False) and isinstance(route, IntentRoute) and route.intentType == "ui-layout")


def normalize_cache_intent(intent: str) -> str:
    return re.sub(r"\s+", " ", str(intent or "").strip()).lower()


def canonical_analysis_intent(intent: str, route: IntentRoute, selected_roles: list[str]) -> str:
    normalized = normalize_cache_intent(intent)
    if route.intentType != "news" and selected_roles != ["news"]:
        return normalized
    compacted = "".join(normalized.split())
    if any(term in compacted for term in ("악재", "부정", "리스크", "하락재료", "bearish", "negative", "risk", "downside")):
        return "news-negative"
    if any(term in compacted for term in ("호재", "긍정", "상승재료", "bullish", "positive", "upside")):
        return "news-positive"
    if any(term in compacted for term in ("실적", "가이던스", "매출", "이익", "earnings", "guidance", "revenue", "profit")):
        return "news-earnings"
    if any(term in compacted for term in ("애널리스트", "목표가", "등급", "투자의견", "analyst", "price target", "pricetarget", "rating", "upgrade", "downgrade")):
        return "news-analyst"
    return "news-overview"

def chart_context_cache_payload(chart_context: dict[str, Any], selected_roles: list[str]) -> dict[str, Any] | None:
    if "chart" not in selected_roles or not isinstance(chart_context, dict):
        return None
    return json.loads(json.dumps(chart_context, sort_keys=True, ensure_ascii=True, default=str))


def references_cache_payload(references: Any) -> list[dict[str, Any]]:
    if not isinstance(references, list):
        return []
    compact = []
    for item in references:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        compact.append({
            "type": item.get("type"),
            "sourcePanelId": item.get("sourcePanelId"),
            "displayLabel": item.get("displayLabel"),
            "symbol": data.get("symbol"),
            "timestamp": data.get("timestamp") or data.get("publishedAt") or data.get("date") or data.get("from"),
            "title": data.get("title"),
            "summary": data.get("summary"),
            "close": data.get("close"),
        })
    return json.loads(json.dumps(compact, sort_keys=True, ensure_ascii=True, default=str))


def operation_ir_cache_payload(operation_ir: Any) -> dict[str, Any]:
    if not isinstance(operation_ir, dict):
        return {}
    payload = {
        "operations": [
            {
                "kind": item.get("kind"),
                "type": item.get("type"),
                "target": item.get("target"),
                "requiredSources": item.get("requiredSources"),
            }
            for item in operation_ir.get("operations", [])
            if isinstance(item, dict)
        ],
        "entities": operation_ir.get("entities") if isinstance(operation_ir.get("entities"), dict) else {},
        "suggestedRoles": operation_ir.get("suggestedRoles") if isinstance(operation_ir.get("suggestedRoles"), list) else [],
    }
    return json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str))


def has_order_or_account_terms(intent: str) -> bool:
    text = str(intent or "").lower()
    compacted = "".join(text.split())
    terms = (
        "주문",
        "매수",
        "매도",
        "계좌",
        "잔고",
        "포지션",
        "체결",
        "order",
        "buy",
        "sell",
        "trade",
        "account",
        "balance",
        "position",
    )
    return any(term in compacted for term in terms)


def is_live_status_intent(intent: str) -> bool:
    text = str(intent or "").lower()
    compacted = "".join(text.split())
    terms = (
        "실시간",
        "스트림",
        "웹소켓",
        "연결상태",
        "라이브피드",
        "livefeed",
        "stream",
        "streaming",
        "websocket",
        "connectionstatus",
    )
    return any(term in compacted for term in terms)


def has_available_analysis_data(payload: CachedAgentAnalysis) -> bool:
    for finding in payload.findings:
        if any(item.status == "available" for item in finding.evidence):
            return True
    return any(item.status == "available" for item in payload.providerEvidence)
