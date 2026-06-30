from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Any

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
)
from .contracts import AgentFinding, AnalysisReport, EvidenceItem, FinalAnswer, IntentRoute, MarketEvent, stable_id, utc_now_iso
from .router import route_intent
from .synthesizer import FinalAnswerSynthesizer
from .ui_intent import route_ui_intent

try:
    from langgraph.graph import END, StateGraph
except Exception:
    END = None
    StateGraph = None


class InMemoryReportStore:
    def __init__(self):
        self._reports: dict[str, AnalysisReport] = {}

    def save(self, report: AnalysisReport) -> AnalysisReport:
        self._reports[report.analysisId] = report
        return report

    def get(self, analysis_id: str) -> AnalysisReport | None:
        return self._reports.get(analysis_id)


class AgentOrchestrator:
    def __init__(self, store: InMemoryReportStore | None = None):
        self.store = store or InMemoryReportStore()
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
            graph.add_node("run_selected_role_agents", self._run_selected_role_agents)
            graph.add_node("verify", self._verify)
            graph.add_node("synthesize_final_answer", self._synthesize_final_answer)
            graph.add_node("decide_notification", self._decide_notification)
            graph.add_node("propose_layout", self._propose_layout)
            graph.set_entry_point("normalize_request")
            graph.add_edge("normalize_request", "route_intent")
            graph.add_edge("route_intent", "run_selected_role_agents")
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
        intent = str(request.get("intent") or request.get("prompt") or latest_message(request.get("messages")) or "analysis")
        explicit_symbol = extract_symbol_from_intent(intent)
        symbol = normalize_symbol(
            explicit_symbol or request.get("symbol") or read_symbol_from_chart_context(request.get("chartContext")) or "AAPL"
        )
        chart_context = sanitize_chart_context_for_symbol(request.get("chartContext"), symbol, bool(explicit_symbol))
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
        )
        return {
            **state,
            "symbol": symbol,
            "intent": intent,
            "events": events,
            "context": context,
            "role_findings": [],
        }

    def _route_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        request = state["request"]
        intent = state["intent"]
        router_mode = str(request.get("routerMode") or "hybrid")
        ui_intent = route_ui_intent(intent, state["context"].layoutContext, router_mode)
        if ui_intent.isUiIntent:
            route = IntentRoute(
                source=ui_intent.source,
                intentType="ui-layout",
                selectedRoles=[],
                confidence=ui_intent.confidence,
                reason=ui_intent.reason,
            )
            return {**state, "route": route, "selected_roles": [], "ui_intent": ui_intent}

        route = route_intent(intent, request.get("agentIds"), router_mode)
        selected_roles = list(route.selectedRoles)
        if not selected_roles:
            requested_roles = resolve_requested_roles(request.get("agentIds"))
            selected_roles = [role for role in ["chart", "news", "macro", "ontology"] if role in requested_roles]
        return {**state, "route": route, "selected_roles": selected_roles, "ui_intent": None}

    def _run_chart(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "chart", self.chart_agent)

    def _run_news(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "news", self.news_agent)

    def _run_macro(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "macro", self.macro_agent)

    def _run_ontology(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run_role_agent(state, "ontology", self.ontology_agent)

    def _run_selected_role_agents(self, state: dict[str, Any]) -> dict[str, Any]:
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
        if is_ui_layout_state(state):
            state["context"].providerEvidence = []
            return {**state, "role_findings": [], "provider_evidence": []}

        context = state["context"]
        role_findings = list(state.get("role_findings", []))
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
        analysis_id = stable_id(
            "analysis",
            {
                "symbol": symbol,
                "intent": intent,
                "events": [event.eventId for event in events],
                "createdAt": request.get("createdAt") or utc_now_iso(),
            },
        )
        if is_ui_layout_state(state):
            final_answer = FinalAnswer(
                title="UI 레이아웃 조정",
                summary="요청한 UI 레이아웃 변경을 준비했습니다.",
                sections=[],
                citations=[],
                limitations=[],
            )
        else:
            final_answer = self.synthesizer.synthesize(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=role_findings,
                provider_evidence=provider_evidence,
            )
        summary = final_answer.summary or build_summary(symbol, role_findings, events)
        return {
            **state,
            "analysis_id": analysis_id,
            "final_answer": final_answer,
            "summary": summary,
        }

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
        )
        return {**state, "layout": layout, "report": report}


def collect_provider_evidence(findings) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    for finding in findings:
        evidence.extend(item for item in finding.evidence if item.provider in {"news", "macro", "ontology"})
    return dedupe_provider_evidence(evidence)


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


def extract_symbol_from_intent(intent: str) -> str | None:
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
    for match in re.finditer(r"(?<![A-Za-z0-9.])([A-Z][A-Z0-9]{0,4}(?:\.[A-Z])?)(?![A-Za-z0-9.])", str(intent or "")):
        token = match.group(1).upper()
        if token not in excluded_tokens:
            return token
    return None
