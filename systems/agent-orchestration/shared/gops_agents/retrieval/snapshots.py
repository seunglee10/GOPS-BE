from __future__ import annotations

import os
import re
import time
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from .bulkhead import ProviderBulkheadRejected, provider_bulkhead
from ..contracts import (
    AgentFinding,
    AgentSignal,
    DataSnapshot,
    EvidenceItem,
    FinalAnswer,
    FinalAnswerSection,
    FinalResponse,
    IntentRoute,
    LatencyStage,
    LatencyTrace,
    ResolvedEntity,
    RoutePlan,
    RuntimePolicy,
    SynthesisInput,
    stable_id,
    utc_now_iso,
)
from ..providers import ClickHouseFinancialProvider, ProviderRequest
from ..security import SanitizationResult, merge_safety_warnings, sanitize_text, sanitize_url, sanitize_value


SNAPSHOT_BUNDLE_BY_INTENT = {
    "investment_opinion": ["market_snapshot", "news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "news_impact_analysis": ["news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "relationship_impact_analysis": ["news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "market_summary": ["market_snapshot", "risk_policy_snapshot"],
    "company_comparison": ["market_snapshot", "news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "financial_analysis": ["financial_snapshot", "risk_policy_snapshot"],
    "financial_comparison": ["financial_snapshot", "financial_peer_snapshot", "risk_policy_snapshot"],
    "financial_news_analysis": ["financial_snapshot", "news_snapshot", "risk_policy_snapshot"],
    "risk_check": ["risk_events_snapshot", "market_snapshot", "risk_policy_snapshot"],
    "general_question": ["risk_policy_snapshot"],
}

ANALYSIS_QUERY_POLICY_BY_TYPE = {
    "price_move_cause": {
        "priority": "P0",
        "anchor_mode": "price_move",
        "composition_strategy": "price_news_market_relationship",
        "snapshot_bundle": ["market_snapshot", "news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    },
    "news_price_mismatch": {
        "priority": "P1",
        "anchor_mode": "news_price_reaction",
        "composition_strategy": "mismatch_expectations_sector_valuation",
        "snapshot_bundle": ["news_snapshot", "market_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    },
    "factor_decomposition": {
        "priority": "P1",
        "anchor_mode": "market_sector_single_name",
        "composition_strategy": "decompose_market_sector_single_name",
        "snapshot_bundle": ["market_snapshot", "relationship_snapshot", "news_snapshot", "risk_policy_snapshot"],
    },
    "reference_anchor_analysis": {
        "priority": "P2",
        "anchor_mode": "selected_reference",
        "composition_strategy": "reference_first_context_join",
        "snapshot_bundle": ["chart_analysis_snapshot", "news_snapshot", "risk_policy_snapshot"],
    },
    "chart_overview": {
        "priority": "P0",
        "anchor_mode": "active_chart",
        "composition_strategy": "deterministic_chart_explanation",
        "snapshot_bundle": ["chart_analysis_snapshot", "risk_policy_snapshot"],
    },
    "impact_mapping": {
        "priority": "P2",
        "anchor_mode": "selected_news_or_theme",
        "composition_strategy": "bounded_related_symbols_and_sectors",
        "snapshot_bundle": ["news_snapshot", "relationship_snapshot", "market_snapshot", "risk_policy_snapshot"],
    },
    "earnings_reaction": {
        "priority": "P4",
        "anchor_mode": "earnings_event",
        "composition_strategy": "financial_news_price_reaction",
        "snapshot_bundle": ["financial_snapshot", "news_snapshot", "market_snapshot", "risk_policy_snapshot"],
    },
    "general": {
        "priority": "P3",
        "anchor_mode": "symbol",
        "composition_strategy": "general_synthesis",
        "snapshot_bundle": [],
    },
}

ANALYSIS_ANSWER_POLICY = {
    "start_with_conclusion": True,
    "max_evidence_bullets": 3,
    "allowed_sections": ["핵심 판단", "판단 근거", "반대로 볼 점", "분석한 지표"],
    "forbid_user_todo_sections": True,
    "do_not_delegate_missing_data": True,
    "show_used_indicators": True,
    "hide_internal_terms": ["GraphDB", "ClickHouse", "Redis", "providerEvidence", "Provider status", "snapshot", "스냅샷"],
    "prohibit_direct_investment_command": True,
}


def runtime_policy_from_env() -> RuntimePolicy:
    return RuntimePolicy(
        max_realtime_llm_calls=int(os.getenv("AGENT_MAX_REALTIME_LLM_CALLS", "2")),
        route_llm_fallback_threshold=float(os.getenv("AGENT_ROUTE_LLM_FALLBACK_THRESHOLD", "0.75")),
        total_timeout_ms=int(os.getenv("AGENT_TOTAL_TIMEOUT_MS", "3000")),
        snapshot_timeout_ms=int(os.getenv("AGENT_SNAPSHOT_TIMEOUT_MS", "700")),
        synthesis_timeout_ms=int(os.getenv("AGENT_SYNTHESIS_TIMEOUT_MS", "5000")),
        graphdb_timeout_ms=int(os.getenv("AGENT_GRAPHDB_TIMEOUT_MS", "500")),
        max_items_per_snapshot=int(os.getenv("AGENT_MAX_ITEMS_PER_SNAPSHOT", "5")),
        max_total_synthesis_evidence_items=int(os.getenv("AGENT_MAX_SYNTHESIS_EVIDENCE_ITEMS", "15")),
        max_synthesis_output_tokens=int(os.getenv("AGENT_MAX_SYNTHESIS_OUTPUT_TOKENS", "350")),
        stream_synthesis_response=bool_env("AGENT_STREAM_SYNTHESIS_RESPONSE", True),
    )


def build_route_plan(run_id: str, route: IntentRoute, context: Any, policy: RuntimePolicy) -> RoutePlan:
    intent = route_plan_intent(route)
    analysis_policy = analysis_query_policy(route, context, intent)
    bundle = snapshot_bundle_for_analysis_policy(intent, context, analysis_policy)
    candidates = [str(context.symbol)] if getattr(context, "symbol", None) else []
    candidates.extend(str(item) for item in getattr(context, "newsSymbols", []) if str(item) not in candidates)
    execution_mode = "parallel_snapshots"
    if route.confidence < policy.route_llm_fallback_threshold and route.source == "strict-llm":
        execution_mode = "degraded_route_llm"
    return RoutePlan(
        run_id=run_id,
        intent=intent,
        route_confidence=float(route.confidence),
        entity_candidates=candidates[:3],
        snapshot_bundle=bundle,
        execution_mode=execution_mode,
        llm_calls_allowed=policy.max_realtime_llm_calls,
        analysisQueryType=str(analysis_policy["analysis_query_type"]),
        priority=str(analysis_policy["priority"]),
        anchorMode=str(analysis_policy["anchor_mode"]),
        compositionStrategy=str(analysis_policy["composition_strategy"]),
        answerPolicy=dict(analysis_policy["answer_policy"]),
    )


def analysis_query_policy(route: IntentRoute, context: Any, route_plan_intent_value: str) -> dict[str, Any]:
    query_type = classify_analysis_query_type(route, context, route_plan_intent_value)
    policy = dict(ANALYSIS_QUERY_POLICY_BY_TYPE.get(query_type, ANALYSIS_QUERY_POLICY_BY_TYPE["general"]))
    return {
        "analysis_query_type": query_type,
        "priority": policy["priority"],
        "anchor_mode": policy["anchor_mode"],
        "composition_strategy": policy["composition_strategy"],
        "snapshot_bundle": list(policy["snapshot_bundle"]),
        "answer_policy": {
            **ANALYSIS_ANSWER_POLICY,
            "analysis_query_type": query_type,
            "composition_strategy": policy["composition_strategy"],
        },
    }


def classify_analysis_query_type(route: IntentRoute, context: Any, route_plan_intent_value: str) -> str:
    text = str(getattr(context, "intent", "") or "").lower()
    compacted = re.sub(r"\s+", "", text)
    references = context_references(context)
    ref_types = {str(item.get("type") or "") for item in references}
    operation_types = operation_types_for_context(context)
    intent_type = str(route.intentType or "").lower()

    if is_impact_mapping_query(text, compacted, ref_types):
        return "impact_mapping"
    if is_factor_decomposition_query(text, compacted):
        return "factor_decomposition"
    if is_news_price_mismatch_query(text, compacted, ref_types):
        return "news_price_mismatch"
    if is_earnings_reaction_query(text, compacted):
        return "earnings_reaction"
    if is_reference_anchor_query(text, compacted, ref_types, operation_types):
        return "reference_anchor_analysis"
    if is_chart_overview_query(context, compacted, intent_type):
        return "chart_overview"
    if is_price_move_cause_query(text, compacted, operation_types, intent_type, route_plan_intent_value):
        return "price_move_cause"
    return "general"


def is_chart_overview_query(context: Any, compacted: str, intent_type: str) -> bool:
    chart_context = getattr(context, "chartContext", {})
    chart_document = chart_context.get("chartDocument") if isinstance(chart_context, dict) else None
    if not isinstance(chart_document, dict) or not chart_document.get("symbol"):
        return False
    return (
        "chart" in intent_type
        or compacted in {"차트분석해줘", "분석해줘", "봐줘", "차트봐줘", "차트설명해줘", "analysis", "analyze"}
    )


def operation_types_for_context(context: Any) -> set[str]:
    operation_ir = getattr(context, "operationIR", {})
    if not isinstance(operation_ir, dict):
        return set()
    operations = operation_ir.get("operations") if isinstance(operation_ir.get("operations"), list) else []
    return {str(item.get("type") or "") for item in operations if isinstance(item, dict)}


def is_impact_mapping_query(text: str, compacted: str, ref_types: set[str]) -> bool:
    has_news_anchor = "news.article" in ref_types or "news.dailySummary" in ref_types or has_any_text(text, ("뉴스", "기사", "headline", "news"))
    return has_news_anchor and has_any_text(
        compacted,
        ("영향받", "영향받는", "관련종목", "관련주", "수혜주", "피해주", "어떤종목", "무슨종목", "관련섹터", "섹터뭐"),
    )


def is_factor_decomposition_query(text: str, compacted: str) -> bool:
    return (
        has_any_text(compacted, ("개별이슈", "시장이슈", "섹터이슈", "공통요인", "개별요인", "시장요인", "섹터요인"))
        or ("개별" in compacted and has_any_text(compacted, ("시장", "섹터", "공통")))
        or has_any_text(text, ("single-name", "market issue", "sector issue"))
    )


def is_news_price_mismatch_query(text: str, compacted: str, ref_types: set[str]) -> bool:
    has_news_anchor = "news.article" in ref_types or "news.dailySummary" in ref_types or has_any_text(text, ("뉴스", "기사", "호재", "headline", "news"))
    positive_news = has_any_text(compacted, ("좋은데", "호재인데", "긍정적인데", "positive", "bullish", "goodnews"))
    negative_price = has_any_text(compacted, ("빠져", "빠졌", "하락", "내려", "떨어", "약세", "down", "drop", "fall"))
    return has_news_anchor and positive_news and negative_price


def is_earnings_reaction_query(text: str, compacted: str) -> bool:
    return has_any_text(text, ("실적", "earnings", "guidance", "가이던스", "eps", "revenue")) and has_any_text(
        compacted,
        ("반응", "왜", "원인", "하락", "상승", "빠져", "올랐", "reaction"),
    )


def is_reference_anchor_query(text: str, compacted: str, ref_types: set[str], operation_types: set[str]) -> bool:
    if not ref_types:
        return False
    if operation_types & {"explain_news", "explain_price_move", "link_news_to_price_move", "explain_recommendation"}:
        return True
    return has_any_text(compacted, ("이뉴스", "이기사", "이봉", "이종목", "이추천", "추천이유", "선택한", "여기", "이거", "저거"))


def is_price_move_cause_query(
    text: str,
    compacted: str,
    operation_types: set[str],
    intent_type: str,
    route_plan_intent_value: str,
) -> bool:
    if operation_types & {"explain_price_move", "link_news_to_price_move"}:
        return True
    if "market-move" in intent_type or route_plan_intent_value == "investment_opinion":
        return has_any_text(compacted, ("왜", "원인", "이유", "급등", "급락", "올랐", "떨어", "하락", "상승", "빠져"))
    return has_any_text(text, ("why up", "why down", "price move", "surge", "selloff"))


def snapshot_bundle_for_analysis_policy(intent: str, context: Any, analysis_policy: dict[str, Any]) -> list[str]:
    query_type = str(analysis_policy.get("analysis_query_type") or "general")
    if query_type == "general":
        bundle = list(SNAPSHOT_BUNDLE_BY_INTENT.get(intent, SNAPSHOT_BUNDLE_BY_INTENT["investment_opinion"]))
    else:
        bundle = list(analysis_policy.get("snapshot_bundle") or [])
    required = operation_required_snapshots(context)
    if required:
        if query_type == "reference_anchor_analysis":
            bundle = required
        else:
            bundle = unique_strings([*bundle, *required])
    bundle = normalize_snapshot_bundle(bundle)
    if query_type in {"chart_overview", "reference_anchor_analysis"}:
        bundle = ["chart_analysis_snapshot" if item == "market_snapshot" else item for item in bundle]
        bundle = unique_strings(bundle)
    return bundle


def operation_required_snapshots(context: Any) -> list[str]:
    operation_ir = getattr(context, "operationIR", {})
    if not isinstance(operation_ir, dict):
        return []
    context_window = operation_ir.get("contextWindow") if isinstance(operation_ir.get("contextWindow"), dict) else {}
    required = [
        str(item)
        for item in context_window.get("requiredSnapshots", [])
        if isinstance(item, (str, int, float))
    ]
    operations = operation_ir.get("operations") if isinstance(operation_ir.get("operations"), list) else []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        for source in operation.get("requiredSources", []) or []:
            snapshot_type = snapshot_type_for_required_source(str(source))
            if snapshot_type:
                required.append(snapshot_type)
    return normalize_snapshot_bundle(required)


def snapshot_type_for_required_source(source: str) -> str | None:
    return {
        "market": "market_snapshot",
        "chart": "chart_analysis_snapshot",
        "macro": "market_snapshot",
        "news": "news_snapshot",
        "ontology": "relationship_snapshot",
        "relationship": "relationship_snapshot",
        "financial": "financial_snapshot",
    }.get(source.strip().lower())


def normalize_snapshot_bundle(bundle: list[str]) -> list[str]:
    normalized = []
    for item in bundle:
        value = {
            "macro_snapshot": "market_snapshot",
            "chart_snapshot": "market_snapshot",
            "ontology_snapshot": "relationship_snapshot",
        }.get(str(item), str(item))
        if value and value not in normalized:
            normalized.append(value)
    if normalized and "risk_policy_snapshot" not in normalized:
        normalized.append("risk_policy_snapshot")
    return normalized


def has_any_text(text: str, tokens: tuple[str, ...]) -> bool:
    return any(str(token).lower() in text for token in tokens)


def route_plan_intent(route: IntentRoute) -> str:
    intent_type = str(route.intentType or "").strip().lower()
    intent_types = {part.strip() for part in intent_type.split("+") if part.strip()}
    roles = list(route.selectedRoles or [])
    if "risk" in roles or "risk-check" in intent_types:
        return "risk_check"
    if "financial" in roles or any("financial" in item for item in intent_types):
        if "news" in roles or "news" in intent_types:
            return "financial_news_analysis"
        if "ontology" in roles or any(item in intent_types for item in {"ontology", "company-comparison", "financial-comparison"}):
            return "financial_comparison"
        return "financial_analysis"
    if intent_types == {"news"} or roles == ["news"]:
        return "news_impact_analysis"
    if intent_types == {"ontology"} or roles == ["ontology"]:
        return "relationship_impact_analysis"
    if intent_types == {"macro"} or roles == ["macro"]:
        return "market_summary"
    if intent_types == {"chart"} or roles == ["chart"]:
        return "market_summary"
    if intent_types == {"market-summary"} or (roles and set(roles).issubset({"chart", "macro"})):
        return "market_summary"
    if "market-move" in intent_types:
        return "investment_opinion"
    if "news" in roles or "ontology" in roles:
        return "investment_opinion"
    return "investment_opinion"


def resolve_entities_for_plan(context: Any, route_plan: RoutePlan) -> list[ResolvedEntity]:
    resolution = getattr(context, "entityResolution", None)
    if isinstance(resolution, dict) and resolution.get("status") == "confirmed" and resolution.get("symbol"):
        symbol = str(resolution.get("symbol") or "").upper()
        matched_alias = str(resolution.get("matchedAlias") or "")
        aliases = [matched_alias] if matched_alias and matched_alias != symbol else []
        return [
            ResolvedEntity(
                raw_name=str(resolution.get("matchedText") or matched_alias or symbol),
                canonical_name=str(resolution.get("canonicalName") or symbol),
                ticker=symbol,
                market="US",
                asset_type="stock",
                graph_node_id=f"company:{symbol}",
                aliases=aliases,
                confidence=float(resolution.get("confidence") or 0.82),
            )
        ]
    values = route_plan.entity_candidates or [str(getattr(context, "symbol", "") or "UNKNOWN")]
    entities = []
    for value in values[:3]:
        text = str(value or "").strip()
        if not text:
            continue
        is_ticker = bool(re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?", text.upper()))
        entities.append(
            ResolvedEntity(
                raw_name=text,
                canonical_name=text.upper() if is_ticker else text,
                ticker=text.upper() if is_ticker else None,
                market="US" if is_ticker else "GLOBAL",
                asset_type="stock" if is_ticker else "sector",
                graph_node_id=f"company:{text.upper()}" if is_ticker else None,
                aliases=[],
                confidence=0.82 if is_ticker else 0.6,
            )
        )
    return entities


class SnapshotExecutor:
    def __init__(self, *, news_agent: Any, ontology_agent: Any):
        self.news = NewsSnapshotProvider(news_agent)
        self.market = MarketSnapshotProvider()
        self.chart = ChartAnalysisSnapshotProvider()
        self.relationship = RelationshipSnapshotProvider(ontology_agent)
        self.financial = FinancialSnapshotProvider()
        self.financial_peer = FinancialPeerSnapshotProvider(self.financial.provider)
        self.risk = RiskPolicySnapshotProvider()
        self.risk_events = RiskEventsSnapshotProvider()

    def fetch(self, *, context: Any, run_id: str, route_plan: RoutePlan, policy: RuntimePolicy) -> list[DataSnapshot]:
        providers = {
            "market_snapshot": self.market,
            "chart_analysis_snapshot": self.chart,
            "news_snapshot": self.news,
            "relationship_snapshot": self.relationship,
            "financial_snapshot": self.financial,
            "financial_peer_snapshot": self.financial_peer,
            "risk_events_snapshot": self.risk_events,
        }
        bundle = [item for item in route_plan.snapshot_bundle if item in providers]
        snapshots_by_type: dict[str, DataSnapshot] = {}
        if bundle:
            started_at = time.perf_counter()
            timeout_seconds = max(0.001, policy.snapshot_timeout_ms / 1000)
            executor = ThreadPoolExecutor(max_workers=len(bundle))
            futures = {
                executor.submit(fetch_provider_snapshot, providers[snapshot_type], snapshot_type, context, run_id, policy.max_items_per_snapshot): snapshot_type
                for snapshot_type in bundle
            }
            try:
                for future in as_completed(futures, timeout=timeout_seconds):
                    snapshot_type = futures[future]
                    try:
                        snapshots_by_type[snapshot_type] = future.result()
                    except Exception as exc:
                        snapshots_by_type[snapshot_type] = error_snapshot(run_id, snapshot_type, exc)
                    if elapsed_ms(started_at) >= policy.snapshot_timeout_ms:
                        break
            except FutureTimeoutError:
                pass
            finally:
                for future, snapshot_type in futures.items():
                    if snapshot_type in snapshots_by_type:
                        continue
                    if future.done():
                        try:
                            snapshots_by_type[snapshot_type] = future.result()
                        except Exception as exc:
                            snapshots_by_type[snapshot_type] = error_snapshot(run_id, snapshot_type, exc)
                    else:
                        future.cancel()
                        snapshots_by_type[snapshot_type] = timeout_snapshot(run_id, snapshot_type, policy.snapshot_timeout_ms)
                executor.shutdown(wait=False, cancel_futures=True)

        ordered = [sanitize_snapshot(snapshots_by_type[item]) for item in bundle if item in snapshots_by_type]
        if "risk_policy_snapshot" in route_plan.snapshot_bundle:
            ordered.append(self.risk.fetch(context, run_id, policy.max_items_per_snapshot, ordered))
        return ordered


def fetch_provider_snapshot(provider: Any, snapshot_type: str, context: Any, run_id: str, max_items: int) -> DataSnapshot:
    bulkhead_name = snapshot_type.replace("_snapshot", "")
    try:
        with provider_bulkhead(bulkhead_name):
            return provider.fetch(context, run_id, max_items)
    except ProviderBulkheadRejected as exc:
        timing = getattr(context, "timing", None)
        if isinstance(timing, dict):
            timing["providerBulkheadRejected"] = int(timing.get("providerBulkheadRejected") or 0) + 1
        return error_snapshot(run_id, snapshot_type, exc)


def news_symbols_for_context(context: Any) -> list[str]:
    symbols = []
    for value in getattr(context, "newsSymbols", []):
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if bool_env("AGENT_EXPANDED_RETRIEVAL_ENABLED", False):
        primary_symbol = str(getattr(context, "symbol", "") or "").strip().upper()
        if primary_symbol and primary_symbol not in symbols:
            symbols.append(primary_symbol)
        retrieval_context = getattr(context, "retrievalContext", None)
        if retrieval_context is not None and hasattr(retrieval_context, "related_symbol_values"):
            for value in retrieval_context.related_symbol_values():
                symbol = str(value or "").strip().upper()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
    return symbols


def market_peer_symbols_for_context(context: Any) -> list[str]:
    if not bool_env("AGENT_EXPANDED_RETRIEVAL_ENABLED", False):
        return []
    retrieval_context = getattr(context, "retrievalContext", None)
    if retrieval_context is None or not hasattr(retrieval_context, "related_symbol_values"):
        return []
    policy = getattr(retrieval_context, "fanout_policy", None)
    max_peers = int(getattr(policy, "max_market_peers", 0) or 0)
    if max_peers <= 0:
        return []
    return retrieval_context.related_symbol_values()[:max_peers]


def financial_peer_symbols_for_context(context: Any) -> list[str]:
    values = []
    for attr in ("relationshipSymbols", "newsSymbols"):
        for value in getattr(context, attr, []) or []:
            symbol = str(value or "").strip().upper()
            if symbol and symbol not in values:
                values.append(symbol)
    return values[:5]


class NewsSnapshotProvider:
    def __init__(self, news_agent: Any):
        self.news_agent = news_agent

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        request = news_provider_request(context)
        provider = self.news_agent.provider
        localizer = self.news_agent.localizer
        daily_summaries = []
        try:
            evidence = provider.fetch(request)
            if hasattr(provider, "fetch_daily_summaries"):
                daily_summaries = list(provider.fetch_daily_summaries(request))
            evidence = localizer.localize(
                symbol=str(context.symbol),
                intent=str(context.intent),
                evidence=evidence,
                allow_runtime_openai=bool_env("AGENT_ALLOW_RUNTIME_NEWS_OPENAI", False),
            )
            evidence = align_news_evidence(evidence, context)
        except Exception as exc:
            evidence = [EvidenceItem.no_data("news", "News snapshot unavailable", f"뉴스 snapshot 조회에 실패했습니다: {exc.__class__.__name__}")]
        reference_evidence = news_reference_evidence(context)
        if reference_evidence:
            evidence = [*reference_evidence, *evidence]
        available = [item for item in evidence if item.provider == "news" and item.status == "available"]
        timing = getattr(context, "timing", None)
        if isinstance(timing, dict):
            timing["newsItemsFetched"] = len(available)
        warnings = []
        if not available and not daily_summaries:
            warnings.append("no_relevant_news_found")
        source = news_snapshot_source(available)
        items = available[:max_items] if available else evidence[:max_items]
        summary = news_snapshot_summary(context, available, daily_summaries)
        snapshot = DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": "news_snapshot", "symbol": context.symbol}),
            run_id=run_id,
            snapshot_type="news_snapshot",
            status="success" if available or daily_summaries else "partial",
            source=source,
            cache_hit=source == "cache",
            freshness=freshness_from_evidence(items),
            summary=summary,
            signals=[news_signal_from_evidence(item, context.symbol) for item in available[:max_items]],
            evidence=items,
            data_quality="high" if available else ("medium" if daily_summaries else "low"),
            confidence=0.72 if available else (0.58 if daily_summaries else 0.35),
            latency_ms=elapsed_ms(started_at),
            warnings=warnings,
        )
        snapshot.daily_summaries = daily_summaries
        sanitized = sanitize_snapshot(snapshot)
        daily_result = sanitize_value(daily_summaries)
        sanitized.daily_summaries = daily_result.value
        if daily_result.warnings:
            sanitized.warnings = merge_safety_warnings([*sanitized.warnings, *daily_result.warnings])
        return sanitized


def news_provider_request(context: Any) -> ProviderRequest:
    base = ProviderRequest(str(context.symbol), str(context.intent), symbols=tuple(news_symbols_for_context(context)))
    reference = next((
        item for item in context_references(context)
        if str(item.get("type") or "") in {"chart.candle", "chart.range", "chart.orderFlow"}
    ), None)
    if not isinstance(reference, dict):
        return base
    data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
    anchor_text = str(data.get("timestamp") or data.get("from") or "")
    try:
        anchor = datetime.fromisoformat(anchor_text.replace("Z", "+00:00"))
    except ValueError:
        return base
    interval = str(data.get("interval") or data.get("timeframe") or "1D")
    before, after = {
        "1m": (timedelta(minutes=60), timedelta(minutes=30)),
        "5m": (timedelta(minutes=60), timedelta(minutes=30)),
        "10m": (timedelta(minutes=60), timedelta(minutes=30)),
        "1h": (timedelta(hours=4), timedelta(hours=2)),
        "4h": (timedelta(days=1), timedelta(days=1)),
        "1D": (timedelta(days=1), timedelta(days=1)),
        "1W": (timedelta(days=7), timedelta(days=7)),
    }.get(interval, (timedelta(days=1), timedelta(days=1)))
    return ProviderRequest(
        base.symbol,
        base.intent,
        symbols=base.symbols,
        fromAt=(anchor - before).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        toAt=(anchor + after).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        availableAsOf=utc_now_iso(),
    )


def align_news_evidence(evidence: list[EvidenceItem], context: Any) -> list[EvidenceItem]:
    reference = next((item for item in context_references(context) if str(item.get("type") or "").startswith("chart.")), None)
    if not isinstance(reference, dict):
        return evidence
    data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
    anchor_text = str(data.get("timestamp") or data.get("from") or "")
    try:
        anchor = datetime.fromisoformat(anchor_text.replace("Z", "+00:00"))
    except ValueError:
        return evidence
    interval = str(data.get("interval") or data.get("timeframe") or "1D")
    anchor_end = anchor + {
        "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "10m": timedelta(minutes=10),
        "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1D": timedelta(days=1), "1W": timedelta(days=7),
    }.get(interval, timedelta(days=1))
    aligned = []
    for item in evidence:
        raw = dict(item.raw) if isinstance(item.raw, dict) else {}
        published_text = str(raw.get("publishedAt") or item.observedAt or "")
        try:
            published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
        except ValueError:
            aligned.append(item)
            continue
        lag = (published - anchor).total_seconds()
        available_text = str(raw.get("availableAt") or published_text)
        try:
            available = datetime.fromisoformat(available_text.replace("Z", "+00:00"))
        except ValueError:
            available = published
        available_after_anchor = available > anchor_end
        raw.update({
            "temporalRelation": "after" if available_after_anchor else "before" if published < anchor else "during" if published <= anchor_end else "after",
            "lagSeconds": round(lag),
            "availabilityLagSeconds": round((available - anchor_end).total_seconds()),
            "causality": "temporal_association_only",
        })
        aligned.append(EvidenceItem(
            provider=item.provider, status=item.status, title=item.title, summary=item.summary,
            observedAt=item.observedAt, url=item.url, raw=raw,
        ))
    return sorted(aligned, key=lambda item: (
        0 if str((item.raw or {}).get("subjectRelevance") or "") in {"primary", "secondary"} else 1,
        abs(float((item.raw or {}).get("lagSeconds") or 0)),
        -float((item.raw or {}).get("importanceScore") or 0),
    ))


class MarketSnapshotProvider:
    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        evidence = []
        for event in getattr(context, "marketEvents", [])[:max_items]:
            evidence.extend(getattr(event, "evidence", []) or [])
        chart_context = getattr(context, "chartContext", {}) if isinstance(getattr(context, "chartContext", {}), dict) else {}
        chart_evidence = chart_context_evidence(context, chart_context)
        if chart_evidence:
            evidence.insert(0, chart_evidence)
        reference_evidence = [*chart_reference_evidence(context), *recommendation_reference_evidence(context)]
        if reference_evidence:
            evidence = [*reference_evidence, *evidence]
        peer_symbols = market_peer_symbols_for_context(context)
        peer_evidence = peer_market_evidence(chart_context, peer_symbols)
        evidence.extend(peer_evidence)
        timing = getattr(context, "timing", None)
        if isinstance(timing, dict):
            timing["marketPeersRequested"] = len(peer_symbols)
            timing["marketPeersFetched"] = len(peer_evidence)
        warnings = [] if evidence else ["market_snapshot_unavailable"]
        if peer_symbols and not peer_evidence:
            warnings.append("market_peer_data_unavailable")
        summary = market_snapshot_summary(context.symbol, evidence, peer_symbols, peer_evidence)
        return sanitize_snapshot(DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": "market_snapshot", "symbol": context.symbol}),
            run_id=run_id,
            snapshot_type="market_snapshot",
            status="success" if evidence else "partial",
            source="computed",
            cache_hit=False,
            freshness=freshness_from_evidence(evidence),
            summary=summary,
            signals=market_signals(context, chart_context, evidence)[:max_items],
            evidence=evidence[:max_items],
            data_quality="medium" if evidence else "low",
            confidence=0.64 if evidence else 0.35,
            latency_ms=elapsed_ms(started_at),
            warnings=warnings,
        ))


class ChartAnalysisSnapshotProvider:
    def __init__(self, storage: Any | None = None):
        self.storage = storage

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        chart_context = getattr(context, "chartContext", {}) if isinstance(getattr(context, "chartContext", {}), dict) else {}
        document = chart_context.get("chartDocument") if isinstance(chart_context.get("chartDocument"), dict) else {}
        symbol = str(document.get("symbol") or getattr(context, "symbol", "UNKNOWN")).upper()
        interval = str(document.get("timeframe") or chart_context.get("interval") or "1D")
        asset = None
        storage_error = None
        canonical_error = None
        asset_excluded_as_future = False
        try:
            storage = self.storage
            if storage is None:
                from ..chart_assets.storage import build_chart_asset_storage_from_env

                storage = build_chart_asset_storage_from_env()
            asset = storage.get(symbol, interval)
            if isinstance(asset, dict) and chart_asset_is_after_analysis_anchor(asset, chart_context, context):
                asset = None
                asset_excluded_as_future = True
        except Exception as exc:
            storage_error = exc.__class__.__name__
        if asset is None and (os.getenv("CLICKHOUSE_HTTP_URL") or os.getenv("CLICKHOUSE_HOST")):
            try:
                asset = canonical_chart_asset_for_context(symbol, interval, chart_context, context)
            except Exception as exc:
                canonical_error = exc.__class__.__name__
        from ..chart_intelligence import build_chart_explanation, chart_explanation_evidence

        explanation = build_chart_explanation(context, asset)
        evidence = [chart_explanation_evidence(explanation), *chart_reference_evidence(context)]
        quality = explanation.get("quality") if isinstance(explanation.get("quality"), dict) else {}
        state = str(quality.get("state") or "unavailable")
        has_screen_candles = bool(chart_context.get("candles"))
        available = asset is not None or has_screen_candles
        warnings = []
        if asset is None:
            warnings.append("chart_asset_unavailable")
        if state in {"partial", "screen_only"}:
            warnings.append("partial_chart_data")
        if quality.get("stale"):
            warnings.append("stale_chart_asset")
        if storage_error:
            warnings.append("chart_asset_storage_unavailable")
        if asset_excluded_as_future:
            warnings.append("future_chart_asset_excluded")
        if canonical_error:
            warnings.append("canonical_chart_recompute_unavailable")
        return sanitize_snapshot(DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": "chart_analysis_snapshot", "symbol": symbol, "interval": interval}),
            run_id=run_id,
            snapshot_type="chart_analysis_snapshot",
            status="success" if asset is not None else ("partial" if available else "failed"),
            source="database" if asset is not None else "screen",
            cache_hit=False,
            freshness={"generated_at": explanation.get("asOf"), "stale": bool(quality.get("stale"))},
            summary=evidence[0].summary,
            signals=[],
            evidence=evidence[:max_items],
            data_quality="high" if state == "full" else ("medium" if available else "low"),
            confidence=0.86 if state == "full" else (0.68 if available else 0.25),
            latency_ms=elapsed_ms(started_at),
            warnings=unique_strings(warnings),
        ))


def chart_asset_is_after_analysis_anchor(asset: dict[str, Any], chart_context: dict[str, Any], context: Any) -> bool:
    analysis_window = chart_context.get("analysisWindow") if isinstance(chart_context.get("analysisWindow"), dict) else {}
    anchor_text = str(analysis_window.get("viewportTo") or "")
    reference = next((item for item in context_references(context) if str(item.get("type") or "").startswith("chart.")), None)
    if isinstance(reference, dict):
        data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
        anchor_text = str(data.get("timestamp") or data.get("to") or data.get("from") or anchor_text)
    as_of_text = str(asset.get("asOf") or "")
    try:
        return datetime.fromisoformat(as_of_text.replace("Z", "+00:00")) > datetime.fromisoformat(anchor_text.replace("Z", "+00:00"))
    except ValueError:
        return False


def canonical_chart_asset_for_context(symbol: str, interval: str, chart_context: dict[str, Any], context: Any) -> dict[str, Any] | None:
    from alfaka.analytics.geometry import analyze_geometry
    from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

    analysis_window = chart_context.get("analysisWindow") if isinstance(chart_context.get("analysisWindow"), dict) else {}
    anchor = str(analysis_window.get("viewportTo") or "") or None
    reference = next((item for item in context_references(context) if str(item.get("type") or "").startswith("chart.")), None)
    if isinstance(reference, dict):
        data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
        anchor = str(data.get("timestamp") or data.get("to") or data.get("from") or anchor or "") or None
    rows = ClickHouseMarketDataProvider().candles(symbol, interval, limit=380, to_time=anchor)
    if len(rows) < 120:
        return None
    analysis = analyze_geometry(symbol, interval, rows)
    indicators = analysis.pop("indicators", {})
    algorithm_version = analysis.pop("algorithmVersion", None)
    return {
        "assetVersion": "geometry", "algorithmVersion": algorithm_version,
        "symbol": symbol, "interval": interval, "asOf": rows[-1].get("timestamp"),
        "coverage": {"state": "full" if len(rows) >= 380 else "partial", "qualityFlags": ["point_in_time_recomputed"]},
        "geometry": analysis, "indicators": indicators,
    }


class RelationshipSnapshotProvider:
    def __init__(self, ontology_agent: Any):
        self.ontology_agent = ontology_agent

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        relationship_symbols = tuple(getattr(context, "relationshipSymbols", []) or ())
        try:
            evidence = self.ontology_agent.provider.fetch(
                ProviderRequest(str(context.symbol), str(context.intent), symbols=relationship_symbols)
            )
        except Exception as exc:
            evidence = [EvidenceItem.no_data("ontology", "Relationship snapshot unavailable", f"관계 snapshot 조회에 실패했습니다: {exc.__class__.__name__}")]
        available = [item for item in evidence if item.provider == "ontology" and item.status == "available"]
        warnings = [] if available else ["no_clear_relationship_path"]
        return sanitize_snapshot(DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": "relationship_snapshot", "symbol": context.symbol}),
            run_id=run_id,
            snapshot_type="relationship_snapshot",
            status="success" if available else "partial",
            source="database" if available else "computed",
            cache_hit=False,
            freshness=freshness_from_evidence(available or evidence),
            summary=relationship_summary(context, available),
            signals=[relationship_signal_from_evidence(item, context.symbol) for item in available[:max_items]],
            evidence=(available or evidence)[:max_items],
            data_quality="high" if available else "low",
            confidence=0.7 if available else 0.3,
            latency_ms=elapsed_ms(started_at),
            warnings=warnings,
        ))


class FinancialSnapshotProvider:
    snapshot_type = "financial_snapshot"
    warning_name = "no_financial_summary"

    def __init__(self, provider: Any | None = None):
        self.provider = provider or ClickHouseFinancialProvider()

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        try:
            evidence = self.provider.fetch(ProviderRequest(str(context.symbol), str(context.intent), symbols=tuple(financial_peer_symbols_for_context(context))))
        except Exception as exc:
            evidence = [EvidenceItem.no_data("financial", "Financial snapshot unavailable", f"재무 snapshot 조회에 실패했습니다: {exc.__class__.__name__}")]
        return self._snapshot(context, run_id, max_items, evidence, started_at)

    def _snapshot(self, context: Any, run_id: str, max_items: int, evidence: list[EvidenceItem], started_at: float) -> DataSnapshot:
        available = [item for item in evidence if item.provider == "financial" and item.status == "available"]
        items = (available or evidence)[:max_items]
        warnings = financial_snapshot_warnings(items, self.warning_name)
        return sanitize_snapshot(DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": self.snapshot_type, "symbol": context.symbol}),
            run_id=run_id,
            snapshot_type=self.snapshot_type,
            status="success" if available and not warnings else ("partial" if evidence else "failed"),
            source="cache" if financial_cache_hit(items) else ("database" if available else "computed"),
            cache_hit=financial_cache_hit(items),
            freshness=freshness_from_evidence(items),
            summary=financial_snapshot_summary(context, items, self.snapshot_type),
            signals=[financial_signal_from_evidence(item, context.symbol) for item in available[:max_items]],
            evidence=items,
            data_quality="high" if available and not warnings else ("medium" if available else "low"),
            confidence=0.72 if available and not warnings else (0.58 if available else 0.3),
            latency_ms=elapsed_ms(started_at),
            warnings=warnings,
        ))


class FinancialPeerSnapshotProvider(FinancialSnapshotProvider):
    snapshot_type = "financial_peer_snapshot"
    warning_name = "no_financial_peer_summary"

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        try:
            evidence = self.provider.fetch_peer(ProviderRequest(str(context.symbol), str(context.intent), symbols=tuple(financial_peer_symbols_for_context(context))))
        except Exception as exc:
            evidence = [EvidenceItem.no_data("financial", "Financial peer snapshot unavailable", f"재무 peer snapshot 조회에 실패했습니다: {exc.__class__.__name__}")]
        return self._snapshot(context, run_id, max_items, evidence, started_at)


class RiskPolicySnapshotProvider:
    def fetch(self, context: Any, run_id: str, max_items: int, snapshots: list[DataSnapshot]) -> DataSnapshot:
        started_at = time.perf_counter()
        warnings = ["investment_advice_limited"]
        for snapshot in snapshots:
            warnings.extend(snapshot.warnings)
        if any(snapshot.status in {"partial", "failed"} for snapshot in snapshots):
            warnings.append("partial_data_used")
        warnings = unique_strings(warnings)[:max_items]
        evidence = [
            EvidenceItem(
                provider="policy",
                status="available",
                title="투자 조언 제한",
                summary="최종 응답은 투자 판단 보조 정보이며 직접적인 매수/매도 명령을 제공하지 않습니다.",
                raw={"source_type": "policy_rule", "warnings": warnings},
            )
        ]
        return DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": "risk_policy_snapshot", "symbol": context.symbol}),
            run_id=run_id,
            snapshot_type="risk_policy_snapshot",
            status="success",
            source="computed",
            cache_hit=False,
            freshness={"generated_at": utc_now_iso(), "stale": False},
            summary="투자 권유 제한, partial data, stale data 정책을 적용합니다.",
            signals=[],
            evidence=evidence,
            data_quality="high",
            confidence=0.85,
            latency_ms=elapsed_ms(started_at),
            warnings=warnings,
        )


class RiskEventsSnapshotProvider:
    """리스크 모니터가 발행한 최근 이벤트를 스냅숏으로 노출 (조회 전용)."""

    def __init__(self, provider: Any = None):
        if provider is None:
            from ..providers import RedisRiskEventsProvider

            provider = RedisRiskEventsProvider()
        self.provider = provider

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        request = ProviderRequest(
            str(getattr(context, "symbol", "") or ""),
            str(getattr(context, "intent", "") or ""),
            symbols=tuple(str(item) for item in getattr(context, "newsSymbols", []) or []),
        )
        evidence = list(self.provider.fetch(request))[:max_items]
        available = [item for item in evidence if item.status == "available"]
        if available:
            summary = f"{request.symbol or '포트폴리오'} 관련 리스크 이벤트 {len(available)}건: {available[0].summary}"
        else:
            summary = f"{request.symbol or '포트폴리오'} 관련 최근 리스크 이벤트가 없습니다. 방어 룰에 걸린 항목이 없습니다."
        return sanitize_snapshot(DataSnapshot(
            snapshot_id=stable_id("snapshot", {"runId": run_id, "type": "risk_events_snapshot", "symbol": request.symbol}),
            run_id=run_id,
            snapshot_type="risk_events_snapshot",
            status="success" if available else "partial",
            source="computed",
            cache_hit=False,
            freshness={"generated_at": utc_now_iso(), "stale": False},
            summary=summary,
            signals=[],
            evidence=evidence,
            data_quality="medium" if available else "low",
            confidence=0.7 if available else 0.4,
            latency_ms=elapsed_ms(started_at),
            warnings=[] if available else ["no_recent_risk_events"],
        ))


def sanitize_snapshot(snapshot: DataSnapshot) -> DataSnapshot:
    summary_result = sanitize_text(snapshot.summary)
    freshness_result = sanitize_value(dict(snapshot.freshness))
    signal_results = [sanitize_agent_signal(item) for item in snapshot.signals]
    evidence_results = [sanitize_evidence_item(item) for item in snapshot.evidence]
    warnings = [
        *snapshot.warnings,
        *summary_result.warnings,
        *freshness_result.warnings,
        *(warning for result in signal_results for warning in result.warnings),
        *(warning for result in evidence_results for warning in result.warnings),
    ]
    sanitized = DataSnapshot(
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
        snapshot_type=snapshot.snapshot_type,
        status=snapshot.status,
        source=snapshot.source,
        cache_hit=snapshot.cache_hit,
        freshness=freshness_result.value if isinstance(freshness_result.value, dict) else {},
        summary=summary_result.value,
        signals=[result.value for result in signal_results],
        evidence=[result.value for result in evidence_results],
        data_quality=snapshot.data_quality,
        confidence=snapshot.confidence,
        latency_ms=snapshot.latency_ms,
        warnings=merge_safety_warnings(warnings),
    )
    daily_summaries = getattr(snapshot, "daily_summaries", None)
    if isinstance(daily_summaries, list):
        daily_result = sanitize_value(daily_summaries)
        sanitized.daily_summaries = daily_result.value
        if daily_result.warnings:
            sanitized.warnings = merge_safety_warnings([*sanitized.warnings, *daily_result.warnings])
    return sanitized


def sanitize_evidence_item(item: EvidenceItem) -> SanitizationResult:
    title_result = sanitize_text(item.title)
    summary_result = sanitize_text(item.summary)
    url_result = sanitize_url(item.url)
    raw_result = sanitize_value(item.raw)
    warnings = merge_safety_warnings([*title_result.warnings, *summary_result.warnings, *url_result.warnings, *raw_result.warnings])
    return SanitizationResult(
        EvidenceItem(
            provider=item.provider,
            status=item.status,
            title=title_result.value,
            summary=summary_result.value,
            observedAt=item.observedAt,
            url=url_result.value,
            raw=raw_result.value if isinstance(raw_result.value, dict) else {},
        ),
        warnings,
    )


def sanitize_agent_signal(item: AgentSignal) -> SanitizationResult:
    target_result = sanitize_text(item.target)
    reasoning_result = sanitize_text(item.reasoning)
    warnings = merge_safety_warnings([*target_result.warnings, *reasoning_result.warnings])
    return SanitizationResult(
        AgentSignal(
            target=target_result.value,
            direction=item.direction,
            horizon=item.horizon,
            strength=item.strength,
            reasoning=reasoning_result.value,
        ),
        warnings,
    )

def build_synthesis_input(
    *,
    run_id: str,
    intent: str,
    original_prompt: str,
    entities: list[ResolvedEntity],
    snapshots: list[DataSnapshot],
    policy: RuntimePolicy,
    route_plan: RoutePlan | None = None,
    cross_signals: list[dict[str, Any]] | None = None,
) -> SynthesisInput:
    missing_data = unique_strings(
        warning
        for snapshot in snapshots
        for warning in snapshot.warnings
        if warning not in {"investment_advice_limited"}
    )
    risk_warnings = unique_strings(
        warning
        for snapshot in snapshots
        if snapshot.snapshot_type == "risk_policy_snapshot"
        for warning in snapshot.warnings
    )
    return SynthesisInput(
        run_id=run_id,
        original_prompt=original_prompt,
        intent=intent,
        entities=entities,
        snapshots=trim_snapshot_evidence(snapshots, policy),
        crossSignals=trim_cross_signals(cross_signals or []),
        missing_data=missing_data,
        risk_warnings=risk_warnings,
        output_policy={
            **(dict(route_plan.answerPolicy) if route_plan is not None else ANALYSIS_ANSWER_POLICY),
            "analysis_query_type": getattr(route_plan, "analysisQueryType", "general") if route_plan is not None else "general",
            "priority": getattr(route_plan, "priority", "P3") if route_plan is not None else "P3",
            "anchor_mode": getattr(route_plan, "anchorMode", "symbol") if route_plan is not None else "symbol",
            "composition_strategy": getattr(route_plan, "compositionStrategy", "general_synthesis") if route_plan is not None else "general_synthesis",
            "max_output_tokens": policy.max_synthesis_output_tokens,
            "require_uncertainty_disclosure": True,
            "prohibit_direct_investment_command": True,
        },
    )


def trim_cross_signals(cross_signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_items = env_int("AGENT_MAX_SYNTHESIS_CROSS_SIGNALS", 6)
    max_chars = env_int("AGENT_MAX_SYNTHESIS_CROSS_SIGNAL_CHARS", 1800)
    ordered = sorted(
        [sanitize_value(dict(item)).value for item in cross_signals if isinstance(item, dict)],
        key=lambda item: float(item.get("confidence") or 0.0),
        reverse=True,
    )[:max_items]
    while ordered and len(json.dumps(ordered, ensure_ascii=False, default=str)) > max_chars:
        ordered.pop()
    return ordered


def final_response_from_answer(
    *,
    run_id: str,
    route_plan: RoutePlan,
    answer: FinalAnswer,
    snapshots: list[DataSnapshot],
    timing: dict[str, Any],
) -> FinalResponse:
    key_points = []
    bullish = []
    bearish = []
    relationship_impacts = []
    for section in answer.sections:
        bullets = list(section.bullets)
        title = section.title.lower()
        if "관계" in section.title or "relationship" in title:
            relationship_impacts.extend(bullets)
        elif "반대" in section.title or "리스크" in section.title or "bear" in title:
            bearish.extend(bullets)
        else:
            key_points.extend(bullets)
    warnings = unique_strings(
        [
            *(warning for snapshot in snapshots for warning in snapshot.warnings),
            *(["llm_call_budget_exceeded"] if int(timing.get("llmBudgetBlocked") or 0) > 0 else []),
        ]
    )
    data_warnings = [warning for warning in warnings if "stale" in warning or "no_" in warning or "unavailable" in warning]
    return FinalResponse(
        run_id=run_id,
        answer_type=final_response_type(route_plan.intent),
        summary=answer.summary,
        key_points=key_points[:5],
        bullish_points=bullish[:5],
        bearish_points=bearish[:5],
        relationship_impacts=relationship_impacts[:5],
        risk_warnings=[warning for warning in warnings if warning not in data_warnings][:5],
        data_freshness_warnings=data_warnings[:5],
        partial_data_used=any(snapshot.status in {"partial", "failed"} for snapshot in snapshots),
        confidence=min((snapshot.confidence for snapshot in snapshots), default=0.5),
        final_stance=stance_for_intent(route_plan.intent),
        latency_ms=float(timing.get("totalMs") or 0.0),
        llm_calls_used=int(timing.get("llmCalls") or 0),
    )


def apply_rule_guardrail(response: FinalResponse, route_plan: RoutePlan, snapshots: list[DataSnapshot]) -> FinalResponse:
    risk_warnings = list(response.risk_warnings)
    data_warnings = list(response.data_freshness_warnings)
    confidence = max(0.0, min(1.0, float(response.confidence)))
    final_stance = response.final_stance
    summary = response.summary
    key_points = list(response.key_points)
    bullish_points = list(response.bullish_points)
    bearish_points = list(response.bearish_points)
    relationship_impacts = list(response.relationship_impacts)

    if response.llm_calls_used > route_plan.llm_calls_allowed:
        risk_warnings.append("llm_call_budget_exceeded")
        confidence = min(confidence, 0.4)
        final_stance = "watch" if route_plan.intent == "investment_opinion" else final_stance

    if any(snapshot.status in {"partial", "failed"} for snapshot in snapshots):
        if "partial_data_used" not in risk_warnings:
            risk_warnings.append("partial_data_used")
        confidence = min(confidence, 0.65)

    combined_text = " ".join([summary, *key_points, *bullish_points, *bearish_points, *relationship_impacts]).lower()
    if contains_direct_investment_command(combined_text):
        risk_warnings.append("direct_investment_command_removed")
        summary = soften_direct_investment_language(summary)
        key_points = [soften_direct_investment_language(item) for item in key_points]
        bullish_points = [soften_direct_investment_language(item) for item in bullish_points]
        bearish_points = [soften_direct_investment_language(item) for item in bearish_points]
        relationship_impacts = [soften_direct_investment_language(item) for item in relationship_impacts]
        final_stance = "watch"
        confidence = min(confidence, 0.55)

    summary_result = sanitize_text(summary)
    key_point_results = [sanitize_text(item) for item in key_points]
    bullish_results = [sanitize_text(item) for item in bullish_points]
    bearish_results = [sanitize_text(item) for item in bearish_points]
    relationship_results = [sanitize_text(item) for item in relationship_impacts]
    risk_warnings.extend(summary_result.warnings)
    risk_warnings.extend(warning for result in key_point_results for warning in result.warnings)
    risk_warnings.extend(warning for result in bullish_results for warning in result.warnings)
    risk_warnings.extend(warning for result in bearish_results for warning in result.warnings)
    risk_warnings.extend(warning for result in relationship_results for warning in result.warnings)

    return FinalResponse(
        run_id=response.run_id,
        answer_type=response.answer_type,
        summary=summary_result.value,
        key_points=[result.value for result in key_point_results],
        bullish_points=[result.value for result in bullish_results],
        bearish_points=[result.value for result in bearish_results],
        relationship_impacts=[result.value for result in relationship_results],
        risk_warnings=merge_safety_warnings(risk_warnings)[:5],
        data_freshness_warnings=unique_strings(data_warnings)[:5],
        partial_data_used=response.partial_data_used or any(snapshot.status in {"partial", "failed"} for snapshot in snapshots),
        confidence=confidence,
        final_stance=final_stance,
        latency_ms=response.latency_ms,
        llm_calls_used=response.llm_calls_used,
    )


def latency_trace_from_timing(run_id: str, timing: dict[str, Any], snapshots: list[DataSnapshot]) -> LatencyTrace:
    snapshot_latency = max((snapshot.latency_ms for snapshot in snapshots), default=0.0)
    snapshot_status = "success"
    if any("timeout" in snapshot.warnings for snapshot in snapshots):
        snapshot_status = "timeout"
    elif any(snapshot.status == "failed" for snapshot in snapshots):
        snapshot_status = "failed"
    elif any(snapshot.status == "partial" for snapshot in snapshots):
        snapshot_status = "partial"
    synthesis_status = "skipped" if float(timing.get("finalAnswerMs") or 0.0) == 0.0 else "success"
    if int(timing.get("llmBudgetBlocked") or 0) > 0:
        synthesis_status = "partial"
    stages = [
        LatencyStage("route_and_plan", float(timing.get("routeAndPlanMs") or 0.0), "success"),
        LatencyStage("entity_resolve", float(timing.get("entityResolveMs") or 0.0), "success"),
        LatencyStage("snapshot_fetch", snapshot_latency, snapshot_status, any(snapshot.cache_hit for snapshot in snapshots)),
        LatencyStage("retrieval_context", float(timing.get("retrievalContextMs") or 0.0), "success", bool(timing.get("graphExpansionCacheHit"))),
        LatencyStage("cross_signal_join", float(timing.get("crossSignalJoinMs") or 0.0), "success"),
        LatencyStage("synthesis_llm", float(timing.get("finalAnswerMs") or 0.0), synthesis_status),
        LatencyStage("guardrail", float(timing.get("guardrailMs") or 0.0), "success"),
    ]
    return LatencyTrace(
        run_id=run_id,
        total_latency_ms=float(timing.get("totalMs") or 0.0),
        llm_calls_used=int(timing.get("llmCalls") or 0),
        stages=stages,
    )


def role_findings_from_snapshots(selected_roles: list[str], snapshots: list[DataSnapshot], context: Any) -> list[AgentFinding]:
    by_type = {snapshot.snapshot_type: snapshot for snapshot in snapshots}
    findings = []
    for role in selected_roles:
        snapshot = snapshot_for_role(role, by_type)
        if snapshot is None:
            findings.append(missing_snapshot_finding(role, context))
            continue
        findings.append(
            AgentFinding(
                agentId=f"{role}-agent",
                role=role_name(role),
                summary=snapshot.summary,
                rationale=snapshot_rationale(snapshot),
                confidence=snapshot.confidence,
                evidence=list(snapshot.evidence),
                tags=[role, "snapshot-provider", snapshot.snapshot_type, snapshot.status],
            )
        )
    return findings


def snapshot_for_role(role: str, by_type: dict[str, DataSnapshot]) -> DataSnapshot | None:
    if role == "chart":
        return by_type.get("chart_analysis_snapshot") or by_type.get("market_snapshot")
    if role == "news":
        return by_type.get("news_snapshot")
    if role == "macro":
        return by_type.get("market_snapshot")
    if role == "ontology":
        return by_type.get("relationship_snapshot")
    if role == "financial":
        return merge_financial_snapshots(by_type)
    if role == "risk":
        return by_type.get("risk_events_snapshot")
    return None


def merge_financial_snapshots(by_type: dict[str, DataSnapshot]) -> DataSnapshot | None:
    primary = by_type.get("financial_snapshot")
    peer = by_type.get("financial_peer_snapshot")
    if primary is None and peer is None:
        return None
    snapshots = [snapshot for snapshot in (primary, peer) if snapshot is not None]
    evidence = [item for snapshot in snapshots for item in snapshot.evidence]
    signals = [item for snapshot in snapshots for item in snapshot.signals]
    summaries = [snapshot.summary for snapshot in snapshots if snapshot.summary]
    status = "partial" if any(snapshot.status != "success" for snapshot in snapshots) else "success"
    return sanitize_snapshot(DataSnapshot(
        snapshot_id=stable_id("snapshot", {"runId": snapshots[0].run_id, "type": "financial_synthetic", "parts": [snapshot.snapshot_type for snapshot in snapshots]}),
        run_id=snapshots[0].run_id,
        snapshot_type="financial_synthetic_snapshot",
        status=status,
        source="computed",
        cache_hit=any(snapshot.cache_hit for snapshot in snapshots),
        freshness=snapshots[0].freshness,
        summary=" ".join(summaries),
        signals=signals,
        evidence=evidence,
        data_quality="low" if any(snapshot.data_quality == "low" for snapshot in snapshots) else "medium",
        confidence=min((snapshot.confidence for snapshot in snapshots), default=0.5),
        latency_ms=max((snapshot.latency_ms for snapshot in snapshots), default=0.0),
        warnings=unique_strings(warning for snapshot in snapshots for warning in snapshot.warnings),
    ))


def role_name(role: str) -> str:
    return {
        "chart": "chart-analysis",
        "news": "news-analysis",
        "macro": "macro-analysis",
        "ontology": "company-relationship-analysis",
        "financial": "financial-analysis",
        "risk": "risk-analysis",
    }.get(role, role)


def snapshot_rationale(snapshot: DataSnapshot) -> str:
    details = [snapshot.summary]
    if snapshot.warnings:
        details.append("warnings=" + ",".join(snapshot.warnings[:3]))
    if snapshot.freshness:
        details.append("freshness=" + str(snapshot.freshness.get("generated_at") or "unknown"))
    return " / ".join(details)


def missing_snapshot_finding(role: str, context: Any) -> AgentFinding:
    return AgentFinding(
        agentId=f"{role}-agent",
        role=role_name(role),
        summary=f"{getattr(context, 'symbol', 'UNKNOWN')} {role} snapshot이 없습니다.",
        rationale="RoutePlan이 선택한 snapshot bundle에 이 role을 구성할 데이터가 없습니다.",
        confidence=0.2,
        evidence=[
            EvidenceItem(
                provider=role,
                status="no-data",
                title=f"{role} snapshot missing",
                summary=f"{role} snapshot provider result is unavailable.",
            )
        ],
        tags=[role, "snapshot-missing"],
    )


def snapshots_from_cached_evidence(run_id: str, context: Any, route_plan: RoutePlan, evidence: list[EvidenceItem]) -> list[DataSnapshot]:
    snapshots = []
    grouped = {
        "news_snapshot": [item for item in evidence if item.provider == "news"],
        "relationship_snapshot": [item for item in evidence if item.provider == "ontology"],
        "market_snapshot": [item for item in evidence if item.provider in {"macro", "market-data"}],
        "chart_analysis_snapshot": [item for item in evidence if item.provider == "chart-analysis"],
        "financial_snapshot": [item for item in evidence if item.provider == "financial" and "peer" not in str(item.title).lower()],
        "financial_peer_snapshot": [item for item in evidence if item.provider == "financial" and "peer" in str(item.title).lower()],
    }
    for snapshot_type in route_plan.snapshot_bundle:
        if snapshot_type == "risk_policy_snapshot":
            continue
        items = grouped.get(snapshot_type, [])
        available = [item for item in items if item.status == "available"]
        if snapshot_type == "chart_analysis_snapshot":
            summary = available[0].summary if available else f"{context.symbol} cached 차트 해설 근거가 없습니다."
            warnings = [] if available else ["chart_asset_unavailable"]
        elif snapshot_type == "news_snapshot":
            summary = news_snapshot_summary(context, available, getattr(context, "newsDailySummaries", []))
            warnings = [] if available else ["no_relevant_news_found"]
        elif snapshot_type == "relationship_snapshot":
            summary = relationship_summary(context, available)
            warnings = [] if available else ["no_clear_relationship_path"]
        elif snapshot_type == "financial_snapshot":
            summary = financial_snapshot_summary(context, items, snapshot_type)
            warnings = financial_snapshot_warnings(items, "no_financial_summary")
        elif snapshot_type == "financial_peer_snapshot":
            summary = financial_snapshot_summary(context, items, snapshot_type)
            warnings = financial_snapshot_warnings(items, "no_financial_peer_summary")
        else:
            summary = f"{context.symbol} cached market snapshot을 재사용했습니다." if items else f"{context.symbol} cached market snapshot이 없습니다."
            warnings = [] if items else ["market_snapshot_unavailable"]
        snapshots.append(
            sanitize_snapshot(DataSnapshot(
                snapshot_id=stable_id("snapshot", {"runId": run_id, "type": snapshot_type, "cached": True}),
                run_id=run_id,
                snapshot_type=snapshot_type,
                status="success" if available else "partial",
                source="cache",
                cache_hit=True,
                freshness=freshness_from_evidence(items),
                summary=summary,
                signals=signals_for_cached_snapshot(snapshot_type, available, context),
                evidence=items[:5],
                data_quality="medium" if items else "low",
                confidence=0.68 if available else 0.35,
                latency_ms=0.0,
                warnings=warnings,
            ))
        )
    if "risk_policy_snapshot" in route_plan.snapshot_bundle:
        snapshots.append(RiskPolicySnapshotProvider().fetch(context, run_id, 5, snapshots))
    return snapshots


def signals_for_cached_snapshot(snapshot_type: str, items: list[EvidenceItem], context: Any) -> list[AgentSignal]:
    if snapshot_type == "news_snapshot":
        return [news_signal_from_evidence(item, context.symbol) for item in items[:5]]
    if snapshot_type == "relationship_snapshot":
        return [relationship_signal_from_evidence(item, context.symbol) for item in items[:5]]
    if snapshot_type in {"financial_snapshot", "financial_peer_snapshot"}:
        return [financial_signal_from_evidence(item, context.symbol) for item in items[:5]]
    return [AgentSignal(target=str(context.symbol), direction="unknown", horizon="unknown", strength="low", reasoning=item.summary) for item in items[:5]]


def chart_context_evidence(context: Any, chart_context: dict[str, Any]) -> EvidenceItem | None:
    chart_document = chart_context.get("chartDocument") if isinstance(chart_context.get("chartDocument"), dict) else {}
    visible_summary = chart_context.get("visibleSummary") if isinstance(chart_context.get("visibleSummary"), dict) else {}
    data_status = chart_context.get("dataStatus") if isinstance(chart_context.get("dataStatus"), dict) else {}
    if not chart_document and not visible_summary and not data_status:
        return None
    return EvidenceItem(
        provider="market-data",
        status="available" if data_status.get("candleCount") else "partial",
        title="Chart market snapshot",
        summary=f"{context.symbol} chart snapshot is available.",
        raw={"chartDocument": chart_document, "visibleSummary": visible_summary, "dataStatus": data_status},
    )


def chart_reference_evidence(context: Any) -> list[EvidenceItem]:
    evidence = []
    for index, reference in enumerate(context_references(context)):
        ref_type = str(reference.get("type") or "")
        if ref_type not in {"chart.candle", "chart.range", "chart.orderFlow", "chart.pattern", "chart.drawing"}:
            continue
        data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
        symbol = str(data.get("symbol") or context.symbol)
        interval = str(data.get("interval") or data.get("timeframe") or "unknown")
        if ref_type == "chart.candle":
            timestamp = str(data.get("timestamp") or data.get("from") or "unknown")
            close = data.get("close")
            summary = f"사용자가 {symbol} {interval} {timestamp} 봉을 분석 기준으로 선택했습니다."
            if isinstance(close, (int, float)):
                summary = f"사용자가 {symbol} {interval} {timestamp} 봉을 선택했고 종가는 {close}입니다."
            title = "Selected chart candle"
        elif ref_type == "chart.range":
            summary = f"사용자가 {symbol} {interval} {data.get('from', 'unknown')}~{data.get('to', 'unknown')} 구간을 분석 기준으로 선택했습니다."
            title = "Selected chart range"
        else:
            anchor_id = data.get("id") or data.get("patternId") or data.get("drawingId") or data.get("timestamp") or "unknown"
            summary = f"사용자가 {symbol} {interval} {ref_type} {anchor_id}을(를) 분석 기준으로 선택했습니다."
            title = f"Selected {ref_type}"
        evidence.append(EvidenceItem(
            provider="market-data",
            status="available",
            title=title,
            summary=summary,
            raw={"referenceIndex": index, "reference": reference},
        ))
    return evidence


def news_reference_evidence(context: Any) -> list[EvidenceItem]:
    evidence = []
    for index, reference in enumerate(context_references(context)):
        ref_type = str(reference.get("type") or "")
        if ref_type not in {"news.article", "news.dailySummary"}:
            continue
        data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
        title = str(data.get("title") or data.get("date") or "Selected news")
        summary = str(data.get("summary") or title)
        observed_at = str(data.get("publishedAt") or data.get("date") or utc_now_iso())
        url = data.get("url") if isinstance(data.get("url"), str) else None
        evidence.append(EvidenceItem(
            provider="news",
            status="available",
            title=title,
            summary=summary,
            observedAt=observed_at,
            url=url,
            raw={"referenceIndex": index, "reference": reference},
        ))
    return evidence


def recommendation_reference_evidence(context: Any) -> list[EvidenceItem]:
    evidence = []
    for index, reference in enumerate(context_references(context)):
        if str(reference.get("type") or "") != "recommendation.stock":
            continue
        data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
        symbol = str(data.get("symbol") or context.symbol).strip().upper()
        rank = data.get("rank")
        score = data.get("score")
        confidence = data.get("confidence")
        reasons = data.get("reasons") if isinstance(data.get("reasons"), list) else []
        reason_texts = [
            str(item.get("text") or "").strip()
            for item in reasons
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        summary_parts = [f"사용자가 {symbol} 추천 종목을 분석 기준으로 선택했습니다."]
        if isinstance(rank, (int, float)):
            summary_parts.append(f"추천 순위는 {int(rank)}위입니다.")
        if isinstance(score, (int, float)):
            summary_parts.append(f"추천 점수는 {float(score):g}입니다.")
        if isinstance(confidence, (int, float)):
            summary_parts.append(f"데이터 신뢰도는 {float(confidence):.0%}입니다.")
        if reason_texts:
            summary_parts.append(f"추천 근거: {' / '.join(reason_texts[:3])}")
        evidence.append(EvidenceItem(
            provider="market-data",
            status="available",
            title=f"Selected stock recommendation: {symbol}",
            summary=" ".join(summary_parts),
            raw={"referenceIndex": index, "reference": reference},
        ))
    return evidence


def context_references(context: Any) -> list[dict[str, Any]]:
    references = getattr(context, "references", [])
    return [item for item in references if isinstance(item, dict)]


def peer_market_evidence(chart_context: dict[str, Any], peer_symbols: list[str]) -> list[EvidenceItem]:
    if not peer_symbols:
        return []
    peer_payload = chart_context.get("peerSummaries") or chart_context.get("relatedMarketSnapshots") or []
    rows = peer_payload.values() if isinstance(peer_payload, dict) else peer_payload
    by_symbol = {}
    for row in rows if isinstance(rows, list) else list(rows):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        if symbol:
            by_symbol[symbol] = row
    evidence = []
    for symbol in peer_symbols:
        row = by_symbol.get(symbol)
        if not row:
            continue
        change = str(row.get("change") or row.get("changePercent") or row.get("percentChange") or "unknown")
        evidence.append(EvidenceItem(
            provider="market-data",
            status="available",
            title=f"{symbol} peer market snapshot",
            summary=f"{symbol} peer market movement is {change}.",
            raw={"peerSymbol": symbol, "peerSummary": row},
        ))
    return evidence


def market_snapshot_summary(symbol: str, evidence: list[EvidenceItem], peer_symbols: list[str], peer_evidence: list[EvidenceItem]) -> str:
    if not evidence:
        return f"{symbol} 시장 snapshot cache가 없습니다."
    if peer_symbols:
        return f"{symbol} 시장/차트 snapshot과 {len(peer_evidence)}/{len(peer_symbols)}개 peer market snapshot을 구성했습니다."
    return f"{symbol} 시장/차트 snapshot을 구성했습니다."


def market_signals(context: Any, chart_context: dict[str, Any], evidence: list[EvidenceItem]) -> list[AgentSignal]:
    visible = chart_context.get("visibleSummary") if isinstance(chart_context.get("visibleSummary"), dict) else {}
    change = str(visible.get("change") or "").strip()
    direction = "unknown"
    if change.startswith("+"):
        direction = "bullish"
    elif change.startswith("-"):
        direction = "bearish"
    signals = [
        AgentSignal(
            target=str(context.symbol),
            direction=direction,
            horizon="intraday",
            strength="medium" if direction != "unknown" else "low",
            reasoning=f"Visible chart change: {change or 'unknown'}.",
        )
    ]
    for item in evidence:
        raw = item.raw if isinstance(item.raw, dict) else {}
        peer_symbol = str(raw.get("peerSymbol") or "").strip().upper()
        if peer_symbol:
            peer_summary = raw.get("peerSummary") if isinstance(raw.get("peerSummary"), dict) else {}
            peer_change = str(peer_summary.get("change") or peer_summary.get("changePercent") or peer_summary.get("percentChange") or "unknown")
            signals.append(AgentSignal(target=peer_symbol, direction="unknown", horizon="intraday", strength="low", reasoning=f"Peer market change: {peer_change}."))
            continue
        event_type = str(raw.get("eventType") or raw.get("event_type") or "").strip()
        if event_type:
            signals.append(AgentSignal(target=str(context.symbol), direction="mixed", horizon="intraday", strength="medium", reasoning=item.summary))
    return signals


def news_signal_from_evidence(item: EvidenceItem, default_target: str) -> AgentSignal:
    raw = item.raw if isinstance(item.raw, dict) else {}
    impact = str(raw.get("impactDirection") or raw.get("sentiment") or "unknown").lower()
    direction = {"positive": "bullish", "negative": "bearish"}.get(impact, impact if impact in {"bullish", "bearish", "mixed", "neutral"} else "unknown")
    return AgentSignal(
        target=str(raw.get("targetSymbol") or raw.get("symbol") or default_target),
        direction=direction,
        horizon="short_term",
        strength=strength_from_score(raw.get("importanceScore") or raw.get("relevanceScore")),
        reasoning=item.summary,
    )


def relationship_signal_from_evidence(item: EvidenceItem, default_target: str) -> AgentSignal:
    raw = item.raw if isinstance(item.raw, dict) else {}
    return AgentSignal(
        target=str(raw.get("ticker") or raw.get("controlledName") or default_target),
        direction="unknown",
        horizon="unknown",
        strength="medium",
        reasoning=item.summary,
    )


def financial_signal_from_evidence(item: EvidenceItem, default_target: str) -> AgentSignal:
    raw = item.raw if isinstance(item.raw, dict) else {}
    metric_count = len(raw.get("metrics") or raw.get("peers") or [])
    return AgentSignal(
        target=str(raw.get("symbol") or default_target),
        direction="unknown",
        horizon="fundamental",
        strength="medium" if metric_count else "low",
        reasoning=item.summary,
    )


def financial_snapshot_summary(context: Any, items: list[EvidenceItem], snapshot_type: str) -> str:
    available = [item for item in items if item.status == "available"]
    if not available:
        return f"{context.symbol} SEC 재무 snapshot 근거가 없습니다."
    if snapshot_type == "financial_peer_snapshot":
        frame_period = next((str((item.raw or {}).get("frame_period") or "") for item in available if isinstance(item.raw, dict)), "")
        suffix = f" 기준 기간 {frame_period}" if frame_period else ""
        return f"{context.symbol} SEC peer 재무 비교 근거를 확인했습니다.{suffix}"
    return available[0].summary or f"{context.symbol} SEC 재무 요약을 확인했습니다."


def financial_snapshot_warnings(items: list[EvidenceItem], empty_warning: str) -> list[str]:
    if not any(item.status == "available" for item in items):
        return [empty_warning]
    warnings = []
    for item in items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        if raw.get("stale"):
            warnings.append("stale_financial_summary")
        quality = raw.get("quality")
        if quality and quality not in {"available"}:
            warnings.append(str(quality))
        for metric in raw.get("metrics") or []:
            if isinstance(metric, dict) and metric.get("quality") and metric.get("quality") != "available":
                warnings.append(str(metric.get("quality")))
    return unique_strings(warnings)


def financial_cache_hit(items: list[EvidenceItem]) -> bool:
    return any(isinstance(item.raw, dict) and (item.raw.get("cache_hit") or item.raw.get("dataSource") == "redis") for item in items)


def news_snapshot_source(items: list[EvidenceItem]) -> str:
    if not items:
        return "database"
    sources = {str((item.raw or {}).get("dataSource") or "").lower() for item in items if isinstance(item.raw, dict)}
    if "prelocalized" in sources:
        return "cache"
    if "alpaca-direct" in sources:
        return "database"
    return "database"


def news_snapshot_summary(context: Any, items: list[EvidenceItem], daily_summaries: list[dict[str, Any]]) -> str:
    if items:
        return f"{context.symbol} 관련 cached news intelligence {len(items)}건을 조회했습니다."
    if daily_summaries:
        return str(daily_summaries[0].get("summary") or f"{context.symbol} 일일 뉴스 요약을 조회했습니다.")
    return f"{context.symbol} 관련 저장 뉴스가 없습니다."


def relationship_summary(context: Any, items: list[EvidenceItem]) -> str:
    if items:
        return f"{context.symbol} 관련 기업 관계/그래프 근거 {len(items)}건을 조회했습니다."
    return f"{context.symbol} 관련 명확한 그래프 관계 경로를 확인하지 못했습니다."


def trim_snapshot_evidence(snapshots: list[DataSnapshot], policy: RuntimePolicy) -> list[DataSnapshot]:
    remaining = policy.max_total_synthesis_evidence_items
    trimmed = []
    for snapshot in snapshots:
        allowed = min(policy.max_items_per_snapshot, max(0, remaining))
        remaining -= allowed
        trimmed.append(
            DataSnapshot(
                snapshot_id=snapshot.snapshot_id,
                run_id=snapshot.run_id,
                snapshot_type=snapshot.snapshot_type,
                status=snapshot.status,
                source=snapshot.source,
                cache_hit=snapshot.cache_hit,
                freshness=dict(snapshot.freshness),
                summary=snapshot.summary,
                signals=list(snapshot.signals[: policy.max_items_per_snapshot]),
                evidence=list(snapshot.evidence[:allowed]),
                data_quality=snapshot.data_quality,
                confidence=snapshot.confidence,
                latency_ms=snapshot.latency_ms,
                warnings=list(snapshot.warnings),
            )
        )
    return trimmed


def freshness_from_evidence(items: list[EvidenceItem]) -> dict[str, Any]:
    observed = next((item.observedAt for item in items if item.observedAt), None)
    generated_at = str(observed or utc_now_iso())
    stale = False
    timestamp = parse_time(generated_at)
    if timestamp:
        stale_after = int(os.getenv("AGENT_SNAPSHOT_STALE_AFTER_SECONDS", "21600"))
        stale = stale_after > 0 and datetime.now(timezone.utc).timestamp() - timestamp > stale_after
    return {"generated_at": generated_at, "stale": stale}


def error_snapshot(run_id: str, snapshot_type: str, exc: Exception) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=stable_id("snapshot", {"runId": run_id, "type": snapshot_type, "error": exc.__class__.__name__}),
        run_id=run_id,
        snapshot_type=snapshot_type,
        status="failed",
        source="computed",
        cache_hit=False,
        freshness={"generated_at": utc_now_iso(), "stale": True},
        summary=f"{snapshot_type} 조회에 실패했습니다.",
        data_quality="low",
        confidence=0.1,
        latency_ms=0.0,
        warnings=[f"{snapshot_type}_failed"],
    )


def timeout_snapshot(run_id: str, snapshot_type: str, timeout_ms: int) -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=stable_id("snapshot", {"runId": run_id, "type": snapshot_type, "timeoutMs": timeout_ms}),
        run_id=run_id,
        snapshot_type=snapshot_type,
        status="failed",
        source="computed",
        cache_hit=False,
        freshness={"generated_at": utc_now_iso(), "stale": True},
        summary=f"{snapshot_type} 조회가 {timeout_ms}ms timeout을 초과했습니다.",
        data_quality="low",
        confidence=0.1,
        latency_ms=float(timeout_ms),
        warnings=[f"{snapshot_type}_timeout", "timeout"],
    )


def final_response_type(intent: str) -> str:
    return {
        "investment_opinion": "investment_opinion",
        "market_summary": "market_summary",
        "relationship_impact_analysis": "relationship_analysis",
        "news_impact_analysis": "news_impact_summary",
    }.get(intent, "general_answer")


def stance_for_intent(intent: str) -> str:
    return "watch" if intent == "investment_opinion" else "not_applicable"


def contains_direct_investment_command(text: str) -> bool:
    terms = (
        "buy now",
        "sell now",
        "place order",
        "지금 매수",
        "바로 매수",
        "매수하세요",
        "지금 매도",
        "바로 매도",
        "매도하세요",
    )
    return any(term in text for term in terms)


def soften_direct_investment_language(text: str) -> str:
    replacements = {
        "지금 매수하세요": "현재 정보 기준으로는 관망 또는 추가 확인이 필요합니다",
        "바로 매수하세요": "현재 정보 기준으로는 관망 또는 추가 확인이 필요합니다",
        "매수하세요": "투자 판단 전 추가 확인이 필요합니다",
        "지금 매도하세요": "현재 정보 기준으로는 리스크 점검이 필요합니다",
        "바로 매도하세요": "현재 정보 기준으로는 리스크 점검이 필요합니다",
        "매도하세요": "투자 판단 전 추가 확인이 필요합니다",
        "buy now": "review the evidence before making any trade",
        "sell now": "review the evidence before making any trade",
        "place order": "review the evidence before making any trade",
    }
    result = str(text)
    for source, target in replacements.items():
        if source.isascii():
            result = re.sub(re.escape(source), target, result, flags=re.IGNORECASE)
        else:
            result = result.replace(source, target)
    return result


def strength_from_score(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "low"
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def unique_strings(values) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def parse_time(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except Exception:
        return default
    return parsed if parsed >= 0 else default
