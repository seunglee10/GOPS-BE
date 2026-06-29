from __future__ import annotations

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
    UnusualEventExplainerAgent,
    VerificationGuardrailAgent,
)
from .contracts import AnalysisReport, EvidenceItem, MarketEvent, stable_id, utc_now_iso
from .router import route_intent
from .synthesizer import FinalAnswerSynthesizer


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
        self.synthesizer = FinalAnswerSynthesizer()

    def analyze(self, request: dict[str, Any]) -> AnalysisReport:
        symbol = normalize_symbol(request.get("symbol") or read_symbol_from_chart_context(request.get("chartContext")) or "AAPL")
        intent = str(request.get("intent") or request.get("prompt") or latest_message(request.get("messages")) or "analysis")
        events = [
            item if isinstance(item, MarketEvent) else MarketEvent.from_dict(item)
            for item in request.get("marketEvents", [])
            if isinstance(item, (dict, MarketEvent))
        ]
        context = AgentContext(
            symbol=symbol,
            intent=intent,
            messages=[item for item in request.get("messages", []) if isinstance(item, dict)],
            chartContext=request.get("chartContext") if isinstance(request.get("chartContext"), dict) else {},
            marketEvents=events,
        )
        route = route_intent(intent, request.get("agentIds"), str(request.get("routerMode") or "hybrid"))
        selected_roles = set(route.selectedRoles) or resolve_requested_roles(request.get("agentIds"))
        visible_agents = [
            ("chart", self.chart_agent),
            ("news", self.news_agent),
            ("macro", self.macro_agent),
            ("ontology", self.ontology_agent),
        ]
        role_findings = [
            agent.analyze(context)
            for role, agent in visible_agents
            if role in selected_roles
        ]
        role_findings.append(self.event_explainer.analyze(context))
        role_findings.append(self.market_summary.analyze(context, role_findings))
        role_findings.append(self.verifier.analyze(context, role_findings))
        provider_evidence = collect_provider_evidence(role_findings)
        context.providerEvidence = provider_evidence
        analysis_id = stable_id(
            "analysis",
            {
                "symbol": symbol,
                "intent": intent,
                "events": [event.eventId for event in events],
                "createdAt": request.get("createdAt") or utc_now_iso(),
            },
        )
        notification = self.notifier.decide(analysis_id, context)
        layout = self.layout_agent.propose(context)
        final_answer = self.synthesizer.synthesize(
            symbol=symbol,
            intent=intent,
            route=route,
            findings=role_findings,
            provider_evidence=provider_evidence,
        )
        summary = final_answer.summary or build_summary(symbol, role_findings, events)
        report = AnalysisReport(
            analysisId=analysis_id,
            symbol=symbol,
            intent=intent,
            status="completed",
            createdAt=utc_now_iso(),
            summary=summary,
            rationale="The conductor routed the request to role agents, composed provider evidence, then generated a final user-facing answer.",
            findings=role_findings,
            marketEvents=events,
            providerEvidence=provider_evidence,
            route=route,
            finalAnswer=final_answer,
            notificationDecision=notification,
            layoutProposal=layout,
            chartProposal=request.get("chartProposal") if isinstance(request.get("chartProposal"), dict) else None,
        )
        return self.store.save(report)

    def get_report(self, analysis_id: str) -> AnalysisReport | None:
        return self.store.get(analysis_id)


def collect_provider_evidence(findings) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    for finding in findings:
        evidence.extend(item for item in finding.evidence if item.provider in {"news", "macro", "ontology"})
    return evidence


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
