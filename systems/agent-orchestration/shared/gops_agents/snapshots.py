from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from .contracts import (
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
from .providers import ProviderRequest


SNAPSHOT_BUNDLE_BY_INTENT = {
    "investment_opinion": ["market_snapshot", "news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "news_impact_analysis": ["news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "relationship_impact_analysis": ["news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "market_summary": ["market_snapshot", "risk_policy_snapshot"],
    "company_comparison": ["market_snapshot", "news_snapshot", "relationship_snapshot", "risk_policy_snapshot"],
    "general_question": ["risk_policy_snapshot"],
}


def runtime_policy_from_env() -> RuntimePolicy:
    return RuntimePolicy(
        max_realtime_llm_calls=int(os.getenv("AGENT_MAX_REALTIME_LLM_CALLS", "1")),
        route_llm_fallback_threshold=float(os.getenv("AGENT_ROUTE_LLM_FALLBACK_THRESHOLD", "0.75")),
        max_items_per_snapshot=int(os.getenv("AGENT_MAX_ITEMS_PER_SNAPSHOT", "5")),
        max_total_synthesis_evidence_items=int(os.getenv("AGENT_MAX_SYNTHESIS_EVIDENCE_ITEMS", "15")),
        max_synthesis_output_tokens=int(os.getenv("AGENT_MAX_SYNTHESIS_OUTPUT_TOKENS", "350")),
        stream_synthesis_response=bool_env("AGENT_STREAM_SYNTHESIS_RESPONSE", True),
    )


def build_route_plan(run_id: str, route: IntentRoute, context: Any, policy: RuntimePolicy) -> RoutePlan:
    intent = route_plan_intent(route)
    bundle = list(SNAPSHOT_BUNDLE_BY_INTENT.get(intent, SNAPSHOT_BUNDLE_BY_INTENT["investment_opinion"]))
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
    )


def route_plan_intent(route: IntentRoute) -> str:
    intent_type = str(route.intentType or "").strip().lower()
    roles = list(route.selectedRoles or [])
    if intent_type == "news" or roles == ["news"]:
        return "news_impact_analysis"
    if intent_type == "ontology" or roles == ["ontology"]:
        return "relationship_impact_analysis"
    if intent_type == "macro" or roles == ["macro"]:
        return "market_summary"
    if intent_type == "chart" or roles == ["chart"]:
        return "market_summary"
    if intent_type == "market-move":
        return "investment_opinion"
    if "news" in roles or "ontology" in roles:
        return "investment_opinion"
    return "investment_opinion"


def resolve_entities_for_plan(context: Any, route_plan: RoutePlan) -> list[ResolvedEntity]:
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
        self.relationship = RelationshipSnapshotProvider(ontology_agent)
        self.risk = RiskPolicySnapshotProvider()

    def fetch(self, *, context: Any, run_id: str, route_plan: RoutePlan, policy: RuntimePolicy) -> list[DataSnapshot]:
        providers = {
            "market_snapshot": self.market,
            "news_snapshot": self.news,
            "relationship_snapshot": self.relationship,
        }
        bundle = [item for item in route_plan.snapshot_bundle if item in providers]
        snapshots_by_type: dict[str, DataSnapshot] = {}
        if bundle:
            with ThreadPoolExecutor(max_workers=len(bundle)) as executor:
                futures = {
                    executor.submit(providers[snapshot_type].fetch, context, run_id, policy.max_items_per_snapshot): snapshot_type
                    for snapshot_type in bundle
                }
                for future in as_completed(futures):
                    snapshot_type = futures[future]
                    try:
                        snapshots_by_type[snapshot_type] = future.result()
                    except Exception as exc:
                        snapshots_by_type[snapshot_type] = error_snapshot(run_id, snapshot_type, exc)

        ordered = [snapshots_by_type[item] for item in bundle if item in snapshots_by_type]
        if "risk_policy_snapshot" in route_plan.snapshot_bundle:
            ordered.append(self.risk.fetch(context, run_id, policy.max_items_per_snapshot, ordered))
        return ordered


class NewsSnapshotProvider:
    def __init__(self, news_agent: Any):
        self.news_agent = news_agent

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        request = ProviderRequest(str(context.symbol), str(context.intent), symbols=tuple(getattr(context, "newsSymbols", [])))
        provider = self.news_agent.provider
        localizer = self.news_agent.localizer
        daily_summaries = []
        try:
            evidence = provider.fetch(request)
            if hasattr(provider, "fetch_daily_summaries"):
                daily_summaries = list(provider.fetch_daily_summaries(request))
                context.newsDailySummaries = daily_summaries
            evidence = localizer.localize(
                symbol=str(context.symbol),
                intent=str(context.intent),
                evidence=evidence,
                allow_runtime_openai=bool_env("AGENT_ALLOW_RUNTIME_NEWS_OPENAI", False),
            )
        except Exception as exc:
            evidence = [EvidenceItem.no_data("news", "News snapshot unavailable", f"뉴스 snapshot 조회에 실패했습니다: {exc.__class__.__name__}")]
        available = [item for item in evidence if item.provider == "news" and item.status == "available"]
        warnings = []
        if not available and not daily_summaries:
            warnings.append("no_relevant_news_found")
        source = news_snapshot_source(available)
        items = available[:max_items] if available else evidence[:max_items]
        summary = news_snapshot_summary(context, available, daily_summaries)
        return DataSnapshot(
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
        warnings = [] if evidence else ["market_snapshot_unavailable"]
        summary = f"{context.symbol} 시장/차트 snapshot을 구성했습니다." if evidence else f"{context.symbol} 시장 snapshot cache가 없습니다."
        return DataSnapshot(
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
        )


class RelationshipSnapshotProvider:
    def __init__(self, ontology_agent: Any):
        self.ontology_agent = ontology_agent

    def fetch(self, context: Any, run_id: str, max_items: int) -> DataSnapshot:
        started_at = time.perf_counter()
        try:
            evidence = self.ontology_agent.provider.fetch(ProviderRequest(str(context.symbol), str(context.intent)))
        except Exception as exc:
            evidence = [EvidenceItem.no_data("ontology", "Relationship snapshot unavailable", f"관계 snapshot 조회에 실패했습니다: {exc.__class__.__name__}")]
        available = [item for item in evidence if item.provider == "ontology" and item.status == "available"]
        warnings = [] if available else ["no_clear_relationship_path"]
        return DataSnapshot(
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
        )


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


def build_synthesis_input(
    *,
    run_id: str,
    intent: str,
    original_prompt: str,
    entities: list[ResolvedEntity],
    snapshots: list[DataSnapshot],
    policy: RuntimePolicy,
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
        missing_data=missing_data,
        risk_warnings=risk_warnings,
        output_policy={
            "max_output_tokens": policy.max_synthesis_output_tokens,
            "require_uncertainty_disclosure": True,
            "prohibit_direct_investment_command": True,
        },
    )


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
    warnings = unique_strings(warning for snapshot in snapshots for warning in snapshot.warnings)
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

    if response.llm_calls_used > route_plan.llm_calls_allowed:
        risk_warnings.append("llm_call_budget_exceeded")
        confidence = min(confidence, 0.4)
        final_stance = "watch" if route_plan.intent == "investment_opinion" else final_stance

    if any(snapshot.status in {"partial", "failed"} for snapshot in snapshots):
        if "partial_data_used" not in risk_warnings:
            risk_warnings.append("partial_data_used")
        confidence = min(confidence, 0.65)

    combined_text = " ".join([summary, *key_points, *response.bullish_points, *response.bearish_points]).lower()
    if contains_direct_investment_command(combined_text):
        risk_warnings.append("direct_investment_command_removed")
        summary = soften_direct_investment_language(summary)
        key_points = [soften_direct_investment_language(item) for item in key_points]
        final_stance = "watch"
        confidence = min(confidence, 0.55)

    return FinalResponse(
        run_id=response.run_id,
        answer_type=response.answer_type,
        summary=summary,
        key_points=key_points,
        bullish_points=list(response.bullish_points),
        bearish_points=list(response.bearish_points),
        relationship_impacts=list(response.relationship_impacts),
        risk_warnings=unique_strings(risk_warnings)[:5],
        data_freshness_warnings=unique_strings(data_warnings)[:5],
        partial_data_used=response.partial_data_used or any(snapshot.status in {"partial", "failed"} for snapshot in snapshots),
        confidence=confidence,
        final_stance=final_stance,
        latency_ms=response.latency_ms,
        llm_calls_used=response.llm_calls_used,
    )


def latency_trace_from_timing(run_id: str, timing: dict[str, Any], snapshots: list[DataSnapshot]) -> LatencyTrace:
    snapshot_latency = max((snapshot.latency_ms for snapshot in snapshots), default=0.0)
    stages = [
        LatencyStage("route_and_plan", float(timing.get("routeAndPlanMs") or 0.0), "success"),
        LatencyStage("entity_resolve", float(timing.get("entityResolveMs") or 0.0), "success"),
        LatencyStage("snapshot_fetch", snapshot_latency, "partial" if any(snapshot.status == "partial" for snapshot in snapshots) else "success", any(snapshot.cache_hit for snapshot in snapshots)),
        LatencyStage("synthesis_llm", float(timing.get("finalAnswerMs") or 0.0), "success"),
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
        return by_type.get("market_snapshot")
    if role == "news":
        return by_type.get("news_snapshot")
    if role == "macro":
        return by_type.get("market_snapshot")
    if role == "ontology":
        return by_type.get("relationship_snapshot")
    return None


def role_name(role: str) -> str:
    return {
        "chart": "chart-analysis",
        "news": "news-analysis",
        "macro": "macro-analysis",
        "ontology": "company-relationship-analysis",
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
    }
    for snapshot_type in route_plan.snapshot_bundle:
        if snapshot_type == "risk_policy_snapshot":
            continue
        items = grouped.get(snapshot_type, [])
        available = [item for item in items if item.status == "available"]
        if snapshot_type == "news_snapshot":
            summary = news_snapshot_summary(context, available, getattr(context, "newsDailySummaries", []))
            warnings = [] if available else ["no_relevant_news_found"]
        elif snapshot_type == "relationship_snapshot":
            summary = relationship_summary(context, available)
            warnings = [] if available else ["no_clear_relationship_path"]
        else:
            summary = f"{context.symbol} cached market snapshot을 재사용했습니다." if items else f"{context.symbol} cached market snapshot이 없습니다."
            warnings = [] if items else ["market_snapshot_unavailable"]
        snapshots.append(
            DataSnapshot(
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
            )
        )
    if "risk_policy_snapshot" in route_plan.snapshot_bundle:
        snapshots.append(RiskPolicySnapshotProvider().fetch(context, run_id, 5, snapshots))
    return snapshots


def signals_for_cached_snapshot(snapshot_type: str, items: list[EvidenceItem], context: Any) -> list[AgentSignal]:
    if snapshot_type == "news_snapshot":
        return [news_signal_from_evidence(item, context.symbol) for item in items[:5]]
    if snapshot_type == "relationship_snapshot":
        return [relationship_signal_from_evidence(item, context.symbol) for item in items[:5]]
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
    }
    result = str(text)
    for source, target in replacements.items():
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
