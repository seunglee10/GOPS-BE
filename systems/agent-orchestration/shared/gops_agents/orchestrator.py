from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import json
import os
import re
import time
from typing import Any

from .analysis_cache import AgentAnalysisCache, CachedAgentAnalysis, analysis_cache_key, build_analysis_cache_from_env
from .agents import (
    AgentContext,
    ChartAgent,
    LayoutAgent,
    MacroAgent,
    MarketSummaryAgent,
    NewsAgent,
    NotificationDecisionAgent,
    OntologyAgent,
    UIAgent,
    UnusualEventExplainerAgent,
    VerificationGuardrailAgent,
    record_news_relevance_counts,
)
from .contracts import AgentFinding, AnalysisReport, EvidenceItem, FinalAnswer, IntentRoute, MarketEvent, stable_id, utc_now_iso
from .cross_signal import CrossSignal, build_cross_signals
from .report_store import InMemoryReportStore, ReportStore
from .retrieval_context import RetrievalContext, build_primary_retrieval_context
from .router import route_intent
from .runtime import RuntimeRunContext
from .snapshots import (
    SnapshotExecutor,
    apply_rule_guardrail,
    build_route_plan,
    build_synthesis_input,
    final_response_from_answer,
    latency_trace_from_timing,
    resolve_entities_for_plan,
    role_findings_from_snapshots,
    runtime_policy_from_env,
    snapshots_from_cached_evidence,
)
from .synthesizer import FinalAnswerSynthesizer
from .ui_intent import route_ui_intent

try:
    from langgraph.graph import END, StateGraph
except Exception:
    END = None
    StateGraph = None


class AgentOrchestrator:
    def __init__(self, store: ReportStore | None = None, analysis_cache: AgentAnalysisCache | None = None):
        self.store = store or InMemoryReportStore()
        self.analysis_cache = analysis_cache if analysis_cache is not None else build_analysis_cache_from_env()
        self.analysis_cache_ttl_seconds = int(os.getenv("AGENT_ANALYSIS_CACHE_TTL_SECONDS", "180"))
        self.analysis_no_data_cache_ttl_seconds = int(os.getenv("AGENT_ANALYSIS_NO_DATA_CACHE_TTL_SECONDS", "60"))
        self.chart_agent = ChartAgent()
        self.news_agent = NewsAgent()
        self.macro_agent = MacroAgent()
        self.ontology_agent = OntologyAgent()
        self.event_explainer = UnusualEventExplainerAgent()
        self.market_summary = MarketSummaryAgent()
        self.verifier = VerificationGuardrailAgent()
        self.notifier = NotificationDecisionAgent()
        self.layout_agent = LayoutAgent()
        self.ui_agent = UIAgent()
        self.synthesizer = FinalAnswerSynthesizer()
        self.runtime_policy = runtime_policy_from_env()
        self.snapshot_executor = SnapshotExecutor(news_agent=self.news_agent, ontology_agent=self.ontology_agent)
        self.workflow = self._build_workflow()

    def analyze(self, request: dict[str, Any]) -> AnalysisReport:
        if self.workflow:
            try:
                state = self.workflow.invoke({"request": request})
            except Exception:
                state = self._run_sequential_workflow(request)
        else:
            state = self._run_sequential_workflow(request)
        return self.store.save(state["report"])

    def get_report(self, analysis_id: str) -> AnalysisReport | None:
        return self.store.get(analysis_id)

    def _build_workflow(self):
        if StateGraph is None or END is None:
            return None
        try:
            graph = StateGraph(dict)
            graph.add_node("normalize_request", self._normalize_request)
            graph.add_node("route_intent", self._route_intent)
            graph.add_node("build_snapshot_plan", self._build_snapshot_plan)
            graph.add_node("build_retrieval_context", self._build_retrieval_context)
            graph.add_node("fetch_data_snapshots", self._fetch_data_snapshots)
            graph.add_node("join_cross_signals", self._join_cross_signals)
            graph.add_node("run_selected_role_agents", self._run_selected_role_agents)
            graph.add_node("verify", self._verify)
            graph.add_node("synthesize_final_answer", self._synthesize_final_answer)
            graph.add_node("decide_notification", self._decide_notification)
            graph.add_node("propose_layout", self._propose_layout)
            graph.set_entry_point("normalize_request")
            graph.add_edge("normalize_request", "route_intent")
            graph.add_edge("route_intent", "build_snapshot_plan")
            graph.add_edge("build_snapshot_plan", "build_retrieval_context")
            graph.add_edge("build_retrieval_context", "fetch_data_snapshots")
            graph.add_edge("fetch_data_snapshots", "join_cross_signals")
            graph.add_edge("join_cross_signals", "run_selected_role_agents")
            graph.add_edge("run_selected_role_agents", "verify")
            graph.add_edge("verify", "synthesize_final_answer")
            graph.add_edge("synthesize_final_answer", "decide_notification")
            graph.add_edge("decide_notification", "propose_layout")
            graph.add_edge("propose_layout", END)
            return graph.compile()
        except Exception:
            return None

    def _run_sequential_workflow(self, request: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {"request": request}
        for node in [
            self._normalize_request,
            self._route_intent,
            self._build_snapshot_plan,
            self._build_retrieval_context,
            self._fetch_data_snapshots,
            self._join_cross_signals,
            self._run_selected_role_agents,
            self._verify,
            self._synthesize_final_answer,
            self._decide_notification,
            self._propose_layout,
        ]:
            state = node(state)
        return state

    def _normalize_request(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        timing = empty_timing()
        runtime_policy = runtime_policy_from_env()
        runtime_context = RuntimeRunContext(policy=runtime_policy, timing=timing)
        intent = str(request.get("intent") or request.get("prompt") or latest_message(request.get("messages")) or "analysis")
        explicit_symbol = extract_symbol_from_intent(intent)
        news_topic = None if explicit_symbol else extract_news_topic_from_intent(intent)
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
        }

    def _route_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        intent = state["intent"]
        router_mode = str(request.get("routerMode") or "hybrid")
        ui_intent = route_ui_intent(intent, state["context"].layoutContext, router_mode, runtime_context=state.get("runtime_context"))
        if ui_intent.isUiIntent:
            route = IntentRoute(
                source=ui_intent.source,
                intentType="ui-layout",
                selectedRoles=[],
                confidence=ui_intent.confidence,
                reason=ui_intent.reason,
            )
            return {**state, "route": route, "selected_roles": [], "ui_intent": ui_intent, "analysis_cacheable": False, "analysis_cache_hit": False}

        route = route_intent(intent, request.get("agentIds"), router_mode, runtime_context=state.get("runtime_context"))
        selected_roles = list(route.selectedRoles)
        if not selected_roles:
            requested_roles = resolve_requested_roles(request.get("agentIds"))
            selected_roles = [role for role in ["chart", "news", "macro", "ontology"] if role in requested_roles]
        state["context"].intentType = route.intentType
        state["context"].selectedRoles = list(selected_roles)
        return self._load_analysis_cache({**state, "route": route, "selected_roles": selected_roles, "ui_intent": None})

    def _build_snapshot_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        if is_ui_layout_state(state):
            return {**state, "runtime_policy": self.runtime_policy, "route_plan": None, "resolved_entities": []}
        started_at = time.perf_counter()
        policy = runtime_policy_from_env()
        runtime_context = state.get("runtime_context")
        if isinstance(runtime_context, RuntimeRunContext):
            runtime_context.refresh_policy(policy)
        route_plan = build_route_plan(state["run_id"], state["route"], state["context"], policy)
        add_timing_ms(state, "routeAndPlanMs", (time.perf_counter() - started_at) * 1000)

        entity_started_at = time.perf_counter()
        resolved_entities = resolve_entities_for_plan(state["context"], route_plan)
        add_timing_ms(state, "entityResolveMs", (time.perf_counter() - entity_started_at) * 1000)
        return {
            **state,
            "runtime_policy": policy,
            "route_plan": route_plan,
            "resolved_entities": resolved_entities,
        }

    def _build_retrieval_context(self, state: dict[str, Any]) -> dict[str, Any]:
        route_plan = state.get("route_plan")
        if is_ui_layout_state(state) or route_plan is None:
            return {**state, "retrieval_context": None}
        started_at = time.perf_counter()
        retrieval_context = build_primary_retrieval_context(state["run_id"], state["context"], route_plan)
        state["context"].retrievalContext = retrieval_context
        expanded_enabled = os.getenv("AGENT_EXPANDED_RETRIEVAL_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        related_symbols = retrieval_context.related_symbol_values() if expanded_enabled else []
        timing = state.get("timing")
        if isinstance(timing, dict):
            timing["graphExpansionCacheHit"] = bool(retrieval_context.graph_expansion.cache_hit)
            timing["relatedSymbolsRequested"] = len(retrieval_context.graph_expansion.related_symbols)
            timing["relatedSymbolsUsed"] = len(related_symbols)
            timing["themesUsed"] = len(retrieval_context.graph_expansion.themes[: retrieval_context.fanout_policy.max_themes]) if expanded_enabled else 0
            timing["fanoutTruncated"] = expanded_enabled and len(retrieval_context.graph_expansion.related_symbols) > len(related_symbols)
        add_timing_ms(state, "retrievalContextMs", (time.perf_counter() - started_at) * 1000)
        return {**state, "retrieval_context": retrieval_context}

    def _fetch_data_snapshots(self, state: dict[str, Any]) -> dict[str, Any]:
        route_plan = state.get("route_plan")
        if is_ui_layout_state(state) or route_plan is None:
            return {**state, "snapshots": []}

        started_at = time.perf_counter()
        if state.get("analysis_cache_hit"):
            snapshots = snapshots_from_cached_evidence(
                state["run_id"],
                state["context"],
                route_plan,
                list(state.get("provider_evidence", [])),
            )
        else:
            executor = SnapshotExecutor(news_agent=self.news_agent, ontology_agent=self.ontology_agent)
            snapshots = executor.fetch(
                context=state["context"],
                run_id=state["run_id"],
                route_plan=route_plan,
                policy=state.get("runtime_policy") or self.runtime_policy,
            )
        add_timing_ms(state, "snapshotFetchMs", (time.perf_counter() - started_at) * 1000)
        for snapshot in snapshots:
            if snapshot.snapshot_type == "news_snapshot":
                daily_summaries = getattr(snapshot, "daily_summaries", None)
                if isinstance(daily_summaries, list):
                    state["context"].newsDailySummaries = [item for item in daily_summaries if isinstance(item, dict)]
                record_news_relevance_counts(state["context"], snapshot.evidence)
        return {**state, "snapshots": snapshots}

    def _join_cross_signals(self, state: dict[str, Any]) -> dict[str, Any]:
        if is_ui_layout_state(state) or not cross_signal_enabled():
            return {**state, "cross_signals": []}
        started_at = time.perf_counter()
        signals = build_cross_signals(
            primary_symbol=state["symbol"],
            snapshots=list(state.get("snapshots", [])),
            retrieval_context=state.get("retrieval_context"),
        )
        timing = state.get("timing")
        if isinstance(timing, dict):
            timing["crossSignals"] = len(signals)
        add_timing_ms(state, "crossSignalJoinMs", (time.perf_counter() - started_at) * 1000)
        return {**state, "cross_signals": signals}

    def _load_analysis_cache(self, state: dict[str, Any]) -> dict[str, Any]:
        cache_key = analysis_cache_key_for_state(state)
        if not cache_key:
            return {**state, "analysis_cacheable": False, "analysis_cache_hit": False, "analysis_cache_key": None}

        try:
            cached = self.analysis_cache.get(cache_key)
        except Exception:
            cached = None
        if cached:
            timing = state["timing"]
            timing["cacheHit"] = True
            timing["cacheLayer"] = "analysis"
            record_news_relevance_counts(state["context"], cached.providerEvidence)
            state["context"].newsDailySummaries = list(cached.dailySummaries or [])
            print(
                f"Agent analysis cache hit: symbol={state['symbol']} roles={','.join(cached.route.selectedRoles)} key={cache_key}",
                flush=True,
            )
            return {
                **state,
                "route": cached.route,
                "selected_roles": list(cached.route.selectedRoles),
                "role_findings": cached.findings,
                "provider_evidence": cached.providerEvidence,
                "final_answer": cached.finalAnswer,
                "summary": cached.summary,
                "daily_summaries": list(cached.dailySummaries or []),
                "analysis_cacheable": True,
                "analysis_cache_hit": True,
                "analysis_cache_key": cache_key,
            }

        print(
            f"Agent analysis cache miss: symbol={state['symbol']} roles={','.join(state.get('selected_roles', []))} key={cache_key}",
            flush=True,
        )
        return {**state, "analysis_cacheable": True, "analysis_cache_hit": False, "analysis_cache_key": cache_key}

    def _run_chart(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "chart", self.chart_agent)

    def _run_news(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "news", self.news_agent)

    def _run_macro(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "macro", self.macro_agent)

    def _run_ontology(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "ontology", self.ontology_agent)

    def _run_selected_role_agents(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("analysis_cache_hit"):
            return state

        selected_roles = [
            role
            for role in ["chart", "news", "macro", "ontology"]
            if role in state.get("selected_roles", [])
        ]
        agents = {
            "chart": self.chart_agent,
            "news": self.news_agent,
            "macro": self.macro_agent,
            "ontology": self.ontology_agent,
        }
        if not selected_roles:
            return {**state, "role_findings": []}

        snapshots = list(state.get("snapshots", []))
        if snapshots and os.getenv("AGENT_USE_SNAPSHOT_HOT_PATH", "true").lower() not in {"0", "false", "no"}:
            started_at = time.perf_counter()
            try:
                return {
                    **state,
                    "role_findings": role_findings_from_snapshots(selected_roles, snapshots, state["context"]),
                }
            finally:
                add_timing_ms(state, "roleAnalysisMs", (time.perf_counter() - started_at) * 1000)

        started_at = time.perf_counter()
        try:
            if len(selected_roles) == 1:
                role = selected_roles[0]
                return {**state, "role_findings": [self._safe_analyze_role(role, agents[role], state["context"])]}

            findings_by_role = {}
            with ThreadPoolExecutor(max_workers=len(selected_roles)) as executor:
                futures = {
                    executor.submit(self._safe_analyze_role, role, agents[role], state["context"]): role
                    for role in selected_roles
                }
                for future in as_completed(futures):
                    findings_by_role[futures[future]] = future.result()
            role_findings = [findings_by_role[role] for role in selected_roles if role in findings_by_role]
            return {**state, "role_findings": role_findings}
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            if is_news_only_state(state):
                fetch_ms = state.get("timing", {}).get("newsFetchMs")
                if isinstance(fetch_ms, (int, float)):
                    elapsed_ms = max(0.0, elapsed_ms - float(fetch_ms))
            add_timing_ms(state, "roleAnalysisMs", elapsed_ms)

    def _safe_analyze_role(self, role: str, agent, context: AgentContext):
        try:
            return agent.analyze(context)
        except Exception as exc:
            return role_agent_error_finding(role, context.symbol, exc)

    def _run_role_agent(self, state: dict[str, Any], role: str, agent) -> dict[str, Any]:
        role_findings = list(state.get("role_findings", []))
        if role in state.get("selected_roles", []):
            role_findings.append(self._safe_analyze_role(role, agent, state["context"]))
        return {**state, "role_findings": role_findings}

    def _verify(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("analysis_cache_hit"):
            provider_evidence = list(state.get("provider_evidence", []))
            state["context"].providerEvidence = provider_evidence
            return {**state, "provider_evidence": provider_evidence}

        if is_ui_layout_state(state):
            state["context"].providerEvidence = []
            return {**state, "role_findings": [], "provider_evidence": []}

        context = state["context"]
        role_findings = list(state.get("role_findings", []))
        apply_role_context_updates(context, role_findings)
        role_findings.append(self.event_explainer.analyze(context))
        role_findings.append(self.market_summary.analyze(context, role_findings))
        role_findings.append(self.verifier.analyze(context, role_findings))
        provider_evidence = collect_provider_evidence(role_findings)
        context.providerEvidence = provider_evidence
        return {**state, "role_findings": role_findings, "provider_evidence": provider_evidence}

    def _synthesize_final_answer(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        symbol = state["symbol"]
        intent = state["intent"]
        events = state["events"]
        role_findings = state["role_findings"]
        provider_evidence = state["provider_evidence"]
        route = state["route"]
        route_plan = state.get("route_plan")
        synthesis_input = None
        if route_plan is not None:
            synthesis_input = build_synthesis_input(
                run_id=state["run_id"],
                intent=route_plan.intent,
                original_prompt=intent,
                entities=list(state.get("resolved_entities", [])),
                snapshots=list(state.get("snapshots", [])),
                policy=state.get("runtime_policy") or self.runtime_policy,
                cross_signals=[item.to_dict() if isinstance(item, CrossSignal) else dict(item) for item in state.get("cross_signals", [])],
            )
        analysis_id = stable_id(
            "analysis",
            {
                "symbol": symbol,
                "intent": intent,
                "events": [event.eventId for event in events],
                "createdAt": request.get("createdAt") or utc_now_iso(),
            },
        )
        analysis_id = str(request.get("analysisId") or request.get("requestId") or analysis_id)
        if state.get("analysis_cache_hit") and state.get("final_answer"):
            final_answer = state["final_answer"]
        elif is_ui_layout_state(state):
            final_answer = FinalAnswer(
                title="UI 레이아웃 조정",
                summary="요청한 UI 레이아웃 변경을 준비했습니다.",
                sections=[],
                citations=[],
                limitations=[],
            )
        else:
            started_at = time.perf_counter()
            try:
                final_answer = self.synthesizer.synthesize(
                    symbol=symbol,
                    intent=intent,
                    route=route,
                    findings=role_findings,
                    provider_evidence=provider_evidence,
                    timing=state.get("timing"),
                    daily_summaries=list(state["context"].newsDailySummaries),
                    synthesis_input=synthesis_input,
                    runtime_context=state.get("runtime_context"),
                )
            finally:
                add_timing_ms(state, "finalAnswerMs", (time.perf_counter() - started_at) * 1000)
        summary = final_answer.summary or build_summary(symbol, role_findings, events)
        next_state = {
            **state,
            "analysis_id": analysis_id,
            "final_answer": final_answer,
            "synthesis_input": synthesis_input,
            "summary": summary,
        }
        self._store_analysis_cache(next_state)
        return next_state

    def _decide_notification(self, state: dict[str, Any]) -> dict[str, Any]:
        notification = self.notifier.decide(state["analysis_id"], state["context"])
        return {**state, "notification": notification}

    def _propose_layout(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        context = state["context"]
        if is_ui_layout_state(state):
            layout = self.ui_agent.propose(context, state["ui_intent"])
            if state.get("final_answer"):
                state["final_answer"].summary = layout.rationale
        else:
            layout = self.layout_agent.propose(context, state.get("route"))
        timing = finalize_timing(state)
        route_plan = state.get("route_plan")
        snapshots = list(state.get("snapshots", []))
        final_response = (
            final_response_from_answer(
                run_id=state["run_id"],
                route_plan=route_plan,
                answer=state["final_answer"],
                snapshots=snapshots,
                timing=timing,
            )
            if route_plan and state.get("final_answer")
            else None
        )
        if final_response is not None and route_plan is not None:
            guardrail_started_at = time.perf_counter()
            final_response = apply_rule_guardrail(final_response, route_plan, snapshots)
            add_timing_ms(state, "guardrailMs", (time.perf_counter() - guardrail_started_at) * 1000)
            timing = finalize_timing(state)
            final_response.latency_ms = float(timing.get("totalMs") or 0.0)
        latency_trace = latency_trace_from_timing(state["run_id"], timing, snapshots)
        agent_trace = build_agent_trace(snapshots, state.get("retrieval_context"), list(state.get("cross_signals", [])))
        report = AnalysisReport(
            analysisId=state["analysis_id"],
            symbol=state["symbol"],
            intent=state["intent"],
            status="completed",
            createdAt=utc_now_iso(),
            summary=state["summary"],
            rationale=(
                "The conductor routed the request to UIAgent for layout-only handling."
                if is_ui_layout_state(state)
                else "The conductor routed the request to role agents, composed provider evidence, then generated a final user-facing answer."
            ),
            findings=state["role_findings"],
            marketEvents=state["events"],
            providerEvidence=state["provider_evidence"],
            route=state["route"],
            finalAnswer=state["final_answer"],
            notificationDecision=state["notification"],
            layoutProposal=layout,
            chartProposal=request.get("chartProposal") if isinstance(request.get("chartProposal"), dict) else None,
            dailySummaries=list(context.newsDailySummaries),
            timing=timing,
            routePlan=route_plan,
            resolvedEntities=list(state.get("resolved_entities", [])),
            snapshots=snapshots,
            synthesisInput=state.get("synthesis_input"),
            finalResponse=final_response,
            latencyTrace=latency_trace,
            agentTrace=agent_trace,
        )
        return {**state, "layout": layout, "report": report}

    def _store_analysis_cache(self, state: dict[str, Any]) -> None:
        if state.get("analysis_cache_hit") or not state.get("analysis_cacheable") or not state.get("analysis_cache_key"):
            return
        route = state.get("route")
        final_answer = state.get("final_answer")
        if not isinstance(route, IntentRoute) or not isinstance(final_answer, FinalAnswer):
            return
        payload = CachedAgentAnalysis(
            route=route,
            findings=list(state.get("role_findings", [])),
            providerEvidence=list(state.get("provider_evidence", [])),
            finalAnswer=final_answer,
            summary=str(state.get("summary") or final_answer.summary),
            dailySummaries=list(state["context"].newsDailySummaries),
        )
        ttl_seconds = self.analysis_cache_ttl_seconds if has_available_analysis_data(payload) else self.analysis_no_data_cache_ttl_seconds
        try:
            self.analysis_cache.set(str(state["analysis_cache_key"]), payload, ttl_seconds)
            print(
                f"Agent analysis cache set: symbol={state['symbol']} roles={','.join(route.selectedRoles)} ttl={ttl_seconds} key={state['analysis_cache_key']}",
                flush=True,
            )
        except Exception:
            return


def collect_provider_evidence(findings) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    for finding in findings:
        evidence.extend(item for item in finding.evidence if item.provider in {"news", "macro", "ontology"})
    return dedupe_provider_evidence(evidence)


def apply_role_context_updates(context: AgentContext, findings) -> None:
    for finding in findings:
        daily_summaries = getattr(finding, "daily_summaries", None)
        if isinstance(daily_summaries, list):
            context.newsDailySummaries = [item for item in daily_summaries if isinstance(item, dict)]


def dedupe_provider_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen = set()
    deduped = []
    for item in items:
        key = (item.provider, item.status, item.title, item.summary, item.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def empty_timing() -> dict[str, Any]:
    return {
        "totalMs": 0.0,
        "queueWaitMs": 0.0,
        "cacheHit": False,
        "cacheLayer": "none",
        "newsFetchMs": 0.0,
        "routeAndPlanMs": 0.0,
        "entityResolveMs": 0.0,
        "retrievalContextMs": 0.0,
        "snapshotFetchMs": 0.0,
        "crossSignalJoinMs": 0.0,
        "roleAnalysisMs": 0.0,
        "finalAnswerMs": 0.0,
        "guardrailMs": 0.0,
        "llmCalls": 0,
        "llmBudgetBlocked": 0,
        "directNewsCount": 0,
        "mentionNewsCount": 0,
        "newsItemsFetched": 0,
        "crossSignals": 0,
        "graphExpansionCacheHit": False,
        "relatedSymbolsRequested": 0,
        "relatedSymbolsUsed": 0,
        "themesUsed": 0,
        "marketPeersRequested": 0,
        "marketPeersFetched": 0,
        "fanoutTruncated": False,
        "hotWorkerSaturation": False,
        "deepWorkerSaturation": False,
        "providerBulkheadRejected": 0,
    }


def add_timing_ms(state: dict[str, Any], key: str, elapsed_ms: float) -> None:
    timing = state.get("timing")
    if not isinstance(timing, dict):
        return
    current = timing.get(key)
    timing[key] = (float(current) if isinstance(current, (int, float)) else 0.0) + elapsed_ms


def finalize_timing(state: dict[str, Any]) -> dict[str, Any]:
    timing = dict(state.get("timing") if isinstance(state.get("timing"), dict) else empty_timing())
    started_at = state.get("timing_started_at")
    if isinstance(started_at, (int, float)):
        timing["totalMs"] = (time.perf_counter() - started_at) * 1000
    for key in ["totalMs", "queueWaitMs", "newsFetchMs", "roleAnalysisMs", "finalAnswerMs", "routeAndPlanMs", "entityResolveMs", "retrievalContextMs", "snapshotFetchMs", "crossSignalJoinMs", "guardrailMs"]:
        value = timing.get(key)
        timing[key] = round(float(value) if isinstance(value, (int, float)) else 0.0, 3)
    for key in ["llmCalls", "llmBudgetBlocked", "directNewsCount", "mentionNewsCount", "newsItemsFetched", "relatedSymbolsRequested", "relatedSymbolsUsed", "themesUsed", "marketPeersRequested", "marketPeersFetched", "crossSignals", "providerBulkheadRejected"]:
        value = timing.get(key)
        timing[key] = int(value) if isinstance(value, (int, float)) else 0
    labels = timing.get("llmCallLabels")
    timing["llmCallLabels"] = [str(item) for item in labels] if isinstance(labels, list) else []
    timing["cacheHit"] = bool(timing.get("cacheHit"))
    timing["cacheLayer"] = str(timing.get("cacheLayer") or "none")
    timing["graphExpansionCacheHit"] = bool(timing.get("graphExpansionCacheHit"))
    timing["fanoutTruncated"] = bool(timing.get("fanoutTruncated"))
    timing["hotWorkerSaturation"] = bool(timing.get("hotWorkerSaturation"))
    timing["deepWorkerSaturation"] = bool(timing.get("deepWorkerSaturation"))
    return timing


def build_agent_trace(snapshots, retrieval_context: RetrievalContext | None = None, cross_signals: list[CrossSignal] | None = None) -> dict[str, Any]:
    visible = [
        snapshot
        for snapshot in snapshots
        if getattr(snapshot, "snapshot_type", "") in {"market_snapshot", "news_snapshot", "relationship_snapshot"}
    ]
    hidden = [snapshot for snapshot in snapshots if getattr(snapshot, "snapshot_type", "") == "risk_policy_snapshot"]
    trace = {
        "visibleSnapshots": [snapshot.to_dict() for snapshot in visible],
        "hiddenSnapshots": [snapshot.snapshot_type for snapshot in hidden],
        "warnings": [
            warning
            for snapshot in snapshots
            for warning in getattr(snapshot, "warnings", [])
        ],
    }
    if retrieval_context is not None:
        trace["retrievalContext"] = retrieval_context.to_dict()
    if cross_signals:
        trace["crossSignals"] = [item.to_dict() if isinstance(item, CrossSignal) else dict(item) for item in cross_signals]
    return trace


def analysis_cache_key_for_state(state: dict[str, Any]) -> str | None:
    if not is_analysis_cacheable_state(state):
        return None
    route = state["route"]
    selected_roles = [role for role in route.selectedRoles if role in {"chart", "news", "macro", "ontology"}]
    payload = {
        "symbol": state["symbol"],
        "intent": canonical_analysis_intent(state["intent"], route, selected_roles),
        "routerMode": str(state["request"].get("routerMode") or "hybrid"),
        "agentIds": normalized_agent_ids(state["request"].get("agentIds")),
        "route": {
            "source": route.source,
            "intentType": route.intentType,
            "selectedRoles": selected_roles,
        },
        "events": [event.eventId for event in state.get("events", [])],
        "chartContext": chart_context_cache_payload(state["context"].chartContext, selected_roles),
        "newsSymbols": list(state.get("news_symbols", [])),
    }
    return analysis_cache_key(symbol=state["symbol"], payload=payload)


def is_analysis_cacheable_state(state: dict[str, Any]) -> bool:
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


def cross_signal_enabled() -> bool:
    return os.getenv("AGENT_CROSS_SIGNAL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


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


def normalized_agent_ids(agent_ids: Any) -> list[str]:
    if not isinstance(agent_ids, list):
        return []
    return sorted({str(item).strip() for item in agent_ids if isinstance(item, str) and item.strip()})


def chart_context_cache_payload(chart_context: dict[str, Any], selected_roles: list[str]) -> dict[str, Any] | None:
    if "chart" not in selected_roles or not isinstance(chart_context, dict):
        return None
    return json.loads(json.dumps(chart_context, sort_keys=True, ensure_ascii=True, default=str))


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


def is_ui_layout_state(state: dict[str, Any]) -> bool:
    ui_intent = state.get("ui_intent")
    return bool(ui_intent and getattr(ui_intent, "isUiIntent", False))


def role_agent_error_finding(role: str, symbol: str, exc: Exception):
    role_to_provider = {
        "news": "news",
        "macro": "macro",
        "ontology": "ontology",
        "chart": "chart",
    }
    provider = role_to_provider.get(role, role)
    return AgentFinding(
        agentId=f"{role}-agent",
        role={
            "chart": "chart-analysis",
            "news": "news-analysis",
            "macro": "macro-analysis",
            "ontology": "company-relationship-analysis",
        }.get(role, role),
        summary=f"{symbol} {role} 분석 중 오류가 발생했습니다.",
        rationale=f"{exc.__class__.__name__}: role agent execution failed.",
        confidence=0.1,
        evidence=[
            EvidenceItem(
                provider=provider,
                status="no-data",
                title=f"{role} agent unavailable",
                summary=f"{role} agent 실행 중 오류가 발생했습니다: {exc.__class__.__name__}",
            )
        ],
        tags=[role, "agent-error"],
    )


def resolve_requested_roles(agent_ids: Any) -> set[str]:
    all_roles = {"chart", "news", "macro", "ontology"}
    if not isinstance(agent_ids, list) or not agent_ids:
        return all_roles

    id_to_role = {
        "agent-01": "chart",
        "agent-02": "news",
        "agent-03": "macro",
        "agent-04": "ontology",
        "chart-agent": "chart",
        "news-agent": "news",
        "macro-agent": "macro",
        "ontology-agent": "ontology",
    }
    roles = {
        role
        for item in agent_ids
        if isinstance(item, str)
        for role in [id_to_role.get(item)]
        if role
    }
    return roles or all_roles


def build_summary(symbol: str, findings, events: list[MarketEvent]) -> str:
    if events:
        strongest = max(events, key=lambda event: {"info": 1, "watch": 2, "alert": 3, "critical": 4}.get(event.severity, 1))
        return f"{symbol} has a {strongest.severity} {strongest.eventType} signal."
    return f"{symbol} multi-agent analysis completed."


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


def normalize_symbol(value: str) -> str:
    normalized = value.strip().upper()
    return normalized or "AAPL"


COMPANY_SYMBOL_ALIASES: tuple[tuple[str, str], ...] = (
    ("apple", "AAPL"),
    ("apple inc", "AAPL"),
    ("애플", "AAPL"),
    ("nvidia", "NVDA"),
    ("nvidia corp", "NVDA"),
    ("엔비디아", "NVDA"),
    ("datadog", "DDOG"),
    ("data dog", "DDOG"),
    ("데이터독", "DDOG"),
    ("oracle", "ORCL"),
    ("오라클", "ORCL"),
    ("tesla", "TSLA"),
    ("테슬라", "TSLA"),
    ("microsoft", "MSFT"),
    ("마이크로소프트", "MSFT"),
    ("마소", "MSFT"),
    ("amazon", "AMZN"),
    ("아마존", "AMZN"),
    ("google", "GOOGL"),
    ("alphabet", "GOOGL"),
    ("구글", "GOOGL"),
    ("알파벳", "GOOGL"),
    ("meta", "META"),
    ("meta platforms", "META"),
    ("메타", "META"),
    ("amd", "AMD"),
    ("advanced micro devices", "AMD"),
    ("브로드컴", "AVGO"),
    ("broadcom", "AVGO"),
    ("intel", "INTC"),
    ("인텔", "INTC"),
    ("netflix", "NFLX"),
    ("넷플릭스", "NFLX"),
    ("palantir", "PLTR"),
    ("팔란티어", "PLTR"),
)


NEWS_TOPIC_BASKETS: tuple[dict[str, Any], ...] = (
    {
        "label": "반도체",
        "aliases": (
            "반도체",
            "반도체주",
            "반도체 섹터",
            "반도체 관련",
            "semiconductor",
            "semiconductors",
            "chip",
            "chips",
            "ai chip",
            "gpu",
            "memory chip",
            "hbm",
            "메모리",
        ),
        "symbols": ("NVDA", "AMD", "AVGO", "TSM", "ASML", "MU", "INTC", "QCOM", "TXN", "ADI", "MRVL", "AMAT", "LRCX", "KLAC"),
    },
    {
        "label": "AI",
        "aliases": ("ai 테마", "ai 관련", "인공지능", "생성형 ai", "artificial intelligence"),
        "symbols": ("NVDA", "MSFT", "GOOGL", "META", "AMZN", "AMD", "AVGO", "PLTR", "ARM"),
    },
    {
        "label": "클라우드",
        "aliases": ("클라우드", "cloud", "saas", "소프트웨어"),
        "symbols": ("MSFT", "AMZN", "GOOGL", "ORCL", "CRM", "DDOG", "SNOW", "NET"),
    },
)


def extract_news_topic_from_intent(intent: str) -> dict[str, Any] | None:
    normalized = normalize_cache_intent(intent)
    compacted = "".join(normalized.split())
    if not is_topic_news_request(normalized, compacted):
        return None
    for topic in NEWS_TOPIC_BASKETS:
        for alias in topic["aliases"]:
            if intent_contains_alias(normalized, compacted, str(alias)):
                return {"label": topic["label"], "symbols": tuple(topic["symbols"])}
    return None


def is_topic_news_request(normalized: str, compacted: str) -> bool:
    news_terms = ("뉴스", "기사", "헤드라인", "news", "article", "headline")
    topic_terms = ("관련", "섹터", "테마", "업종", "industry", "sector", "theme")
    return any(term in compacted for term in news_terms) and (
        any(term in compacted for term in topic_terms)
        or any(intent_contains_alias(normalized, compacted, str(alias)) for topic in NEWS_TOPIC_BASKETS for alias in topic["aliases"])
    )


def intent_contains_alias(normalized: str, compacted: str, alias: str) -> bool:
    normalized_alias = normalize_cache_intent(alias)
    if re.fullmatch(r"[a-z0-9 .&-]+", normalized_alias):
        pattern = re.escape(normalized_alias).replace(r"\ ", r"\s+")
        return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", normalized))
    return "".join(normalized_alias.split()) in compacted


def extract_symbol_alias_from_intent(intent: str) -> str | None:
    normalized = normalize_cache_intent(intent)
    compacted = "".join(normalized.split())
    for alias, symbol in COMPANY_SYMBOL_ALIASES:
        if intent_contains_alias(normalized, compacted, alias):
            return symbol
    return None


def extract_symbol_from_intent(intent: str) -> str | None:
    alias_symbol = extract_symbol_alias_from_intent(intent)
    if alias_symbol:
        return alias_symbol

    excluded_tokens = {
        "AI",
        "API",
        "CEO",
        "CFO",
        "ETF",
        "GDP",
        "KST",
        "MVP",
        "PER",
        "PBR",
        "RAG",
        "ROI",
        "USD",
    }
    ambiguous_lowercase_words = {
        "a",
        "all",
        "are",
        "at",
        "ball",
        "best",
        "bro",
        "cat",
        "cost",
        "day",
        "de",
        "do",
        "d",
        "f",
        "fix",
        "for",
        "fox",
        "has",
        "hi",
        "it",
        "key",
        "ko",
        "low",
        "now",
        "o",
        "on",
        "or",
        "q",
        "so",
        "sw",
        "t",
        "tell",
        "to",
        "v",
    }
    known_symbols = known_agent_symbols()
    for match in re.finditer(r"(?<![A-Za-z0-9.])([A-Za-z][A-Za-z0-9]{0,4}(?:\.[A-Za-z])?)(?![A-Za-z0-9.])", str(intent or "")):
        raw_token = match.group(1)
        token = raw_token.upper()
        if token in excluded_tokens:
            continue
        if raw_token != token and raw_token.lower() in ambiguous_lowercase_words:
            continue
        if token in known_symbols:
            return token
    return None


@lru_cache(maxsize=1)
def known_agent_symbols() -> frozenset[str]:
    symbols = {symbol for _alias, symbol in COMPANY_SYMBOL_ALIASES}
    for topic in NEWS_TOPIC_BASKETS:
        symbols.update(str(symbol).upper() for symbol in topic["symbols"])
    try:
        from alfaka.alpaca.subscription import configured_universe_symbols

        symbols.update(configured_universe_symbols())
    except Exception:
        pass
    symbols.update({"XLV"})
    return frozenset(symbols)
