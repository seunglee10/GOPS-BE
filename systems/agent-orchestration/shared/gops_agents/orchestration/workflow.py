from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time
from typing import Any

from ..contracts import AnalysisReport, FinalAnswer, IntentRoute, stable_id, utc_now_iso
from ..retrieval.context import build_primary_retrieval_context
from ..retrieval.cross_signal import CrossSignal, build_cross_signals
from ..retrieval.snapshots import (
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
from ..roles import (
    ChartAgent,
    MacroAgent,
    MarketSummaryAgent,
    NewsAgent,
    OntologyAgent,
    UIAgent,
    UnusualEventExplainerAgent,
    VerificationGuardrailAgent,
    record_news_relevance_counts,
)
from ..runtime import RuntimeRunContext
from ..runtime.analysis_cache import AgentAnalysisCache, CachedAgentAnalysis, build_analysis_cache_from_env
from ..runtime.report_store import InMemoryReportStore, ReportStore
from ..security import merge_safety_warnings
from ..synthesis import FinalAnswerSynthesizer
from .cache import (
    analysis_cache_key_for_state,
    has_available_analysis_data,
    is_news_only_state,
    is_ui_layout_state,
)
from .request import normalize_request_state
from .reporting import (
    apply_role_context_updates,
    build_agent_trace,
    build_summary,
    collect_provider_evidence,
)
from .routing import route_intent
from .roles import role_agent_error_finding
from .timing import add_timing_ms, finalize_timing
from .ui_intent import UIIntent

try:
    from langgraph.graph import END, StateGraph
except Exception:
    END = None
    StateGraph = None


UI_LAYOUT_ACK_SUMMARY = "변경했습니다."
MULTI_AGENT_UI_SIDE_EFFECT_BLOCKED_SUMMARY = "채팅 모드에서는 UI 변경을 실행하지 않습니다."


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
            graph.add_node("propose_layout", self._propose_layout)
            graph.add_node("ui_layout_ack", self._build_ui_layout_ack)
            graph.add_node("finalize_report", self._finalize_report)

            graph.set_entry_point("normalize_request")

            graph.add_edge("normalize_request", "route_intent")
            graph.add_conditional_edges(
                "route_intent",
                route_after_intent,
                {
                    "ui_layout_ack": "ui_layout_ack",
                    "build_snapshot_plan": "build_snapshot_plan",
                },
            )
            graph.add_edge("build_snapshot_plan", "build_retrieval_context")
            graph.add_edge("build_retrieval_context", "fetch_data_snapshots")
            graph.add_edge("fetch_data_snapshots", "join_cross_signals")
            graph.add_edge("join_cross_signals", "run_selected_role_agents")
            graph.add_edge("run_selected_role_agents", "verify")
            graph.add_edge("verify", "synthesize_final_answer")
            graph.add_conditional_edges(
                "synthesize_final_answer",
                route_after_synthesis,
                {
                    "propose_layout": "propose_layout",
                    "finalize_report": "finalize_report",
                },
            )
            graph.add_edge("propose_layout", "finalize_report")
            graph.add_edge("ui_layout_ack", END)
            graph.add_edge("finalize_report", END)
            return graph.compile()
        except Exception:
            return None

    def _run_sequential_workflow(self, request: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {"request": request}
        state = self._normalize_request(state)
        state = self._route_intent(state)
        if should_return_ui_layout_ack(state):
            return self._build_ui_layout_ack(state)
        for node in [
            self._build_snapshot_plan,
            self._build_retrieval_context,
            self._fetch_data_snapshots,
            self._join_cross_signals,
            self._run_selected_role_agents,
            self._verify,
            self._synthesize_final_answer,
        ]:
            state = node(state)
        if should_propose_layout(state):
            state = self._propose_layout(state)
        state = self._finalize_report(state)
        return state

    def _normalize_request(self, state: dict[str, Any]) -> dict[str, Any]:
        return normalize_request_state(state)

    def _route_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        intent = state["intent"]
        router_mode = str(request.get("routerMode") or "hybrid")
        understanding = state.get("query_understanding") if isinstance(state.get("query_understanding"), dict) else {}
        route_mode = str(understanding.get("routeMode") or state.get("route_mode") or "analysis")
        ui_intent = ui_intent_from_understanding(understanding)
        ui_tasks = [task for task in understanding.get("uiTasks", []) if isinstance(task, dict)]
        allow_layout_side_effects = not is_multi_agent_chat_state(state)
        hybrid_ui_intent = ui_intent if route_mode == "hybrid" and allow_layout_side_effects else None
        hybrid_ui_tasks = ui_tasks if route_mode == "hybrid" and allow_layout_side_effects else []
        if is_unsupported_subject_state(state):
            validation = state.get("subject_validation") if isinstance(state.get("subject_validation"), dict) else {}
            route = IntentRoute(
                source="subject-validation",
                intentType="unsupported-company",
                selectedRoles=[],
                confidence=1.0,
                reason=str(validation.get("reason") or "Unsupported company subject."),
            )
            state["context"].intentType = route.intentType
            state["context"].selectedRoles = []
            return {
                **state,
                "route": route,
                "selected_roles": [],
                "ui_intent": hybrid_ui_intent,
                "ui_tasks": hybrid_ui_tasks,
                "analysis_cacheable": False,
                "analysis_cache_hit": False,
            }
        if route_mode == "clarify":
            route = IntentRoute(
                source=str(understanding.get("source") or "query-understanding"),
                intentType="clarify",
                selectedRoles=[],
                confidence=float(understanding.get("confidence") or 0.0),
                reason="Query understanding requires clarification before selecting analysis agents.",
            )
            state["context"].intentType = route.intentType
            state["context"].selectedRoles = []
            return {
                **state,
                "route": route,
                "selected_roles": [],
                "ui_intent": ui_intent if ui_intent is not None and allow_layout_side_effects else None,
                "ui_tasks": ui_tasks if allow_layout_side_effects else [],
                "analysis_cacheable": False,
                "analysis_cache_hit": False,
            }
        if route_mode == "ui_layout" and ui_intent is not None:
            route = IntentRoute(
                source=ui_intent.source or str(understanding.get("source") or "query-understanding"),
                intentType="ui-layout",
                selectedRoles=[],
                confidence=ui_intent.confidence,
                reason=ui_intent.reason,
            )
            state["context"].intentType = route.intentType
            state["context"].selectedRoles = []
            return {
                **state,
                "route": route,
                "selected_roles": [],
                "ui_intent": ui_intent if allow_layout_side_effects else None,
                "ui_tasks": ui_tasks if allow_layout_side_effects else [],
                "route_mode": route_mode,
                "analysis_cacheable": False,
                "analysis_cache_hit": False,
            }

        selected_roles = [role for role in understanding.get("selectedRoles", []) if role in {"chart", "news", "macro", "ontology"}]
        if selected_roles:
            route = IntentRoute(
                source=str(understanding.get("source") or "query-understanding"),
                intentType=str(understanding.get("intentType") or "general-analysis"),
                selectedRoles=selected_roles,
                confidence=float(understanding.get("confidence") or 0.6),
                reason="Merged parallel query understanding into analysis route.",
            )
        else:
            route = route_intent(intent, router_mode=router_mode, runtime_context=state.get("runtime_context"))
        selected_roles = list(route.selectedRoles)
        state["context"].intentType = route.intentType
        state["context"].selectedRoles = list(selected_roles)
        if state.get("analysis_mode") == "multi_agent":
            allow_role_answer_llm_calls(state.get("runtime_context"), len(selected_roles))
        return self._load_analysis_cache({
            **state,
            "route": route,
            "selected_roles": selected_roles,
            "ui_intent": hybrid_ui_intent,
            "ui_tasks": hybrid_ui_tasks,
            "route_mode": route_mode,
        })

    def _build_snapshot_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        if is_terminal_no_analysis_state(state):
            return {**state, "runtime_policy": self.runtime_policy, "route_plan": None, "resolved_entities": []}
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
        if is_terminal_no_analysis_state(state) or state.get("analysis_mode") == "multi_agent" or is_ui_layout_state(state) or route_plan is None:
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
        if state.get("analysis_mode") != "multi_agent" and snapshots and os.getenv("AGENT_USE_SNAPSHOT_HOT_PATH", "true").lower() not in {"0", "false", "no"}:
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

    def _safe_analyze_role(self, role: str, agent, context):
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
        if is_terminal_no_analysis_state(state):
            state["context"].providerEvidence = []
            return {**state, "role_findings": [], "provider_evidence": []}
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
        elif is_unsupported_subject_state(state):
            final_answer = unsupported_subject_final_answer(symbol, state.get("subject_validation"))
        elif is_clarify_state(state):
            final_answer = clarify_final_answer()
        elif is_multi_agent_chat_state(state) and is_ui_layout_state(state):
            final_answer = FinalAnswer(
                title="멀티 에이전트 채팅",
                summary=MULTI_AGENT_UI_SIDE_EFFECT_BLOCKED_SUMMARY,
                sections=[],
                citations=[],
                limitations=[],
            )
        elif is_ui_layout_state(state):
            final_answer = FinalAnswer(
                title="UI 레이아웃 조정",
                summary=UI_LAYOUT_ACK_SUMMARY,
                sections=[],
                citations=[],
                limitations=[],
            )
        elif state.get("analysis_mode") == "multi_agent":
            agent_answers = self._synthesize_agent_answers(state, role_findings)
            state["agent_answers"] = agent_answers
            final_answer = FinalAnswer(
                title="멀티 에이전트 분석",
                summary="각 에이전트가 독립 답변을 작성했습니다.",
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

    def _propose_layout(self, state: dict[str, Any]) -> dict[str, Any]:
        if is_multi_agent_chat_state(state):
            return {**state, "layout": None}
        context = state["context"]
        ui_tasks = list(state.get("ui_tasks", [])) if isinstance(state.get("ui_tasks"), list) else []
        if is_ui_layout_state(state):
            layout = self.ui_agent.propose_many(context, ui_tasks) if ui_tasks else self.ui_agent.propose(context, state["ui_intent"])
            if state.get("final_answer"):
                state["final_answer"].summary = layout.rationale
                state["summary"] = layout.rationale
        elif state.get("route_mode") == "hybrid" and state.get("ui_intent") is not None:
            layout = self.ui_agent.propose_many(context, ui_tasks) if ui_tasks else self.ui_agent.propose(context, state["ui_intent"])
        else:
            layout = None
        return {**state, "layout": layout}

    def _build_ui_layout_ack(self, state: dict[str, Any]) -> dict[str, Any]:
        context = state["context"]
        context.providerEvidence = []
        ui_tasks = list(state.get("ui_tasks", [])) if isinstance(state.get("ui_tasks"), list) else []
        layout = self.ui_agent.propose_many(context, ui_tasks) if ui_tasks else self.ui_agent.propose(context, state["ui_intent"])
        timing = finalize_timing(state)
        analysis_id = analysis_id_for_state(state)
        agent_trace = build_agent_trace(
            [],
            None,
            [],
            state.get("entity_resolution"),
            state.get("query_understanding"),
        )
        agent_trace["analysisMode"] = str(state.get("analysis_mode") or "auto")
        agent_trace["uiLayoutFastAck"] = True
        if state.get("input_safety_warnings"):
            agent_trace["inputGuardrail"] = {"warnings": list(state.get("input_safety_warnings", []))}
        report = AnalysisReport(
            analysisId=analysis_id,
            symbol=state["symbol"],
            intent=state["intent"],
            status="completed",
            createdAt=utc_now_iso(),
            summary=UI_LAYOUT_ACK_SUMMARY,
            rationale="The conductor returned a layout-only acknowledgement without final report synthesis.",
            findings=[],
            marketEvents=state["events"],
            providerEvidence=[],
            route=state.get("route"),
            finalAnswer=None,
            notificationDecision=None,
            layoutProposal=layout,
            chartProposal=(
                state["request"].get("chartProposal")
                if isinstance(state["request"].get("chartProposal"), dict)
                else None
            ),
            dailySummaries=[],
            timing=timing,
            routePlan=None,
            resolvedEntities=[],
            snapshots=[],
            synthesisInput=None,
            finalResponse=None,
            latencyTrace=None,
            agentAnswers=[],
            agentTrace=agent_trace,
        )
        return {
            **state,
            "analysis_id": analysis_id,
            "summary": UI_LAYOUT_ACK_SUMMARY,
            "layout": layout,
            "role_findings": [],
            "provider_evidence": [],
            "snapshots": [],
            "final_answer": None,
            "final_response": None,
            "report": report,
        }

    def _finalize_report(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        context = state["context"]
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
            if state.get("input_safety_warnings"):
                final_response.risk_warnings = merge_safety_warnings([
                    *final_response.risk_warnings,
                    *list(state.get("input_safety_warnings", [])),
                ])[:5]
            add_timing_ms(state, "guardrailMs", (time.perf_counter() - guardrail_started_at) * 1000)
            timing = finalize_timing(state)
            final_response.latency_ms = float(timing.get("totalMs") or 0.0)
        latency_trace = latency_trace_from_timing(state["run_id"], timing, snapshots)
        agent_trace = build_agent_trace(
            snapshots,
            state.get("retrieval_context"),
            list(state.get("cross_signals", [])),
            state.get("entity_resolution"),
            state.get("query_understanding"),
        )
        agent_trace["analysisMode"] = str(state.get("analysis_mode") or "auto")
        if state.get("input_safety_warnings"):
            agent_trace["inputGuardrail"] = {"warnings": list(state.get("input_safety_warnings", []))}
        if state.get("analysis_mode") == "multi_agent":
            agent_trace["multiAgent"] = {
                "answerCount": len(state.get("agent_answers", [])),
                "mergeSynthesisSkipped": True,
            }
        report = AnalysisReport(
            analysisId=state["analysis_id"],
            symbol=state["symbol"],
            intent=state["intent"],
            status="completed",
            createdAt=utc_now_iso(),
            summary=state["summary"],
            rationale=report_rationale_for_state(state),
            findings=state["role_findings"],
            marketEvents=state["events"],
            providerEvidence=state["provider_evidence"],
            route=state["route"],
            finalAnswer=state["final_answer"],
            notificationDecision=state.get("notification"),
            layoutProposal=state.get("layout"),
            chartProposal=request.get("chartProposal") if isinstance(request.get("chartProposal"), dict) else None,
            dailySummaries=list(context.newsDailySummaries),
            timing=timing,
            routePlan=route_plan,
            resolvedEntities=list(state.get("resolved_entities", [])),
            snapshots=snapshots,
            synthesisInput=state.get("synthesis_input"),
            finalResponse=final_response,
            latencyTrace=latency_trace,
            agentAnswers=list(state.get("agent_answers", [])),
            agentTrace=agent_trace,
        )
        return {**state, "report": report}

    def _synthesize_agent_answers(self, state: dict[str, Any], role_findings: list[Any]) -> list[Any]:
        started_at = time.perf_counter()
        selected_finding_roles = {
            role_finding_name(role)
            for role in state.get("selected_roles", [])
        }
        findings = [
            finding
            for finding in role_findings
            if getattr(finding, "agentId", "")
            and getattr(finding, "role", "") in selected_finding_roles
        ]
        if not findings:
            return []
        answers_by_agent: dict[str, Any] = {}
        max_workers = max(1, len(findings))
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self.synthesizer.synthesize_agent_answer,
                        symbol=state["symbol"],
                        intent=state["intent"],
                        finding=finding,
                        timing=state.get("timing"),
                        runtime_context=state.get("runtime_context"),
                    ): finding.agentId
                    for finding in findings
                }
                for future in as_completed(futures):
                    answers_by_agent[futures[future]] = future.result()
        finally:
            add_timing_ms(state, "roleAnswerMs", (time.perf_counter() - started_at) * 1000)
        return [answers_by_agent[finding.agentId] for finding in findings if finding.agentId in answers_by_agent]

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


def cross_signal_enabled() -> bool:
    return os.getenv("AGENT_CROSS_SIGNAL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def analysis_id_for_state(state: dict[str, Any]) -> str:
    request = state["request"]
    return str(
        request.get("analysisId")
        or request.get("requestId")
        or stable_id(
            "analysis",
            {
                "symbol": state["symbol"],
                "intent": state["intent"],
                "events": [event.eventId for event in state.get("events", [])],
                "createdAt": request.get("createdAt") or utc_now_iso(),
            },
        )
    )


def is_multi_agent_chat_state(state: dict[str, Any]) -> bool:
    return str(state.get("analysis_mode") or "").strip() == "multi_agent"


def allows_layout_side_effects(state: dict[str, Any]) -> bool:
    return not is_multi_agent_chat_state(state) and not is_terminal_no_analysis_state(state)


def should_return_ui_layout_ack(state: dict[str, Any]) -> bool:
    return bool(
        allows_layout_side_effects(state)
        and is_ui_layout_state(state)
        and state.get("ui_intent") is not None
    )


def route_after_intent(state: dict[str, Any]) -> str:
    return "ui_layout_ack" if should_return_ui_layout_ack(state) else "build_snapshot_plan"


def report_rationale_for_state(state: dict[str, Any]) -> str:
    if is_multi_agent_chat_state(state):
        return "The conductor handled the request in multi-agent chat mode without UI layout side effects."
    if is_ui_layout_state(state):
        return "The conductor routed the request to UIAgent for layout-only handling."
    return "The conductor routed the request to role agents, composed provider evidence, then generated a final user-facing answer."


def is_unsupported_subject_state(state: dict[str, Any]) -> bool:
    validation = state.get("subject_validation")
    return isinstance(validation, dict) and validation.get("status") == "unsupported"


def is_clarify_state(state: dict[str, Any]) -> bool:
    route = state.get("route")
    return isinstance(route, IntentRoute) and route.intentType == "clarify"


def is_terminal_no_analysis_state(state: dict[str, Any]) -> bool:
    return is_unsupported_subject_state(state) or is_clarify_state(state)


def should_propose_layout(state: dict[str, Any]) -> bool:
    if is_multi_agent_chat_state(state):
        return False
    if is_terminal_no_analysis_state(state):
        return False
    if is_ui_layout_state(state):
        return True
    if str(state.get("route_mode") or "") != "hybrid":
        return False
    ui_intent = state.get("ui_intent")
    return bool(ui_intent and getattr(ui_intent, "isUiIntent", False))


def route_after_synthesis(state: dict[str, Any]) -> str:
    return "propose_layout" if should_propose_layout(state) else "finalize_report"


def allow_role_answer_llm_calls(runtime_context: Any, selected_role_count: int) -> None:
    if runtime_context is None or not hasattr(runtime_context, "llm_budget"):
        return
    budget = runtime_context.llm_budget
    current = int(getattr(budget, "max_calls", 0) or 0)
    used = int(getattr(budget, "used_calls", 0) or 0)
    budget.max_calls = max(current, used + max(1, selected_role_count))


def role_finding_name(role: str) -> str:
    return {
        "chart": "chart-analysis",
        "news": "news-analysis",
        "macro": "macro-analysis",
        "ontology": "company-relationship-analysis",
    }.get(str(role), str(role))


def unsupported_subject_final_answer(symbol: str, validation: Any) -> FinalAnswer:
    payload = validation if isinstance(validation, dict) else {}
    raw_name = str(payload.get("rawName") or payload.get("symbol") or symbol or "UNKNOWN")
    if raw_name == "UNKNOWN":
        title = "기업 인식 실패"
        summary = "입력한 내용을 지원 기업으로 인식하지 못했습니다. 지원 기업 목록에 있는 기업명이나 티커를 입력해 주세요."
    else:
        title = "지원되지 않는 기업"
        summary = f"{raw_name}은 현재 지원 기업 목록에 없습니다. 지원 기업 목록에 추가된 뒤 분석할 수 있습니다."
    return FinalAnswer(
        title=title,
        summary=summary,
        sections=[],
        citations=[],
        limitations=["지원 기업 목록 밖의 기업은 에이전트 분석 provider를 호출하지 않습니다."],
    )


def clarify_final_answer() -> FinalAnswer:
    return FinalAnswer(
        title="추가 확인 필요",
        summary="요청의 분석 대상이나 작업이 명확하지 않아 에이전트를 선택하지 않았습니다. 기업명과 원하는 분석 종류를 함께 입력해 주세요.",
        sections=[],
        citations=[],
        limitations=["불명확한 요청은 LLM intent fallback을 거친 뒤에도 확정되지 않으면 broad analysis로 강제 실행하지 않습니다."],
    )


def ui_intent_from_understanding(understanding: dict[str, Any]) -> UIIntent | None:
    ui_tasks = understanding.get("uiTasks")
    if not isinstance(ui_tasks, list) or not ui_tasks:
        return None
    task = ui_tasks[0]
    if not isinstance(task, dict):
        return None
    return UIIntent(
        isUiIntent=True,
        intentKind="layout",
        targetPanelType=str(task.get("targetPanelType") or "") or None,
        targetPanelId=str(task.get("targetPanelId") or "") or None,
        action=str(task.get("action") or "focus"),
        sizeIntent=str(task.get("sizeIntent") or "") or None,
        positionIntent=str(task.get("positionIntent") or "") or None,
        confidence=float(task.get("confidence") or understanding.get("confidence") or 0.7),
        reason=str(task.get("reason") or "UI task from parallel query understanding."),
        source=str(task.get("source") or understanding.get("source") or "query-understanding"),
    )
