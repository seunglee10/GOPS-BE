from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import AgentFinding, EvidenceItem, LayoutProposal, MarketEvent, NotificationDecision, stable_id, utc_now_iso
from .providers import ClickHouseNewsProvider, EmptyMacroProvider, EmptyOntologyProvider, ProviderRequest


@dataclass
class AgentContext:
    symbol: str
    intent: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    chartContext: dict[str, Any] = field(default_factory=dict)
    marketEvents: list[MarketEvent] = field(default_factory=list)
    providerEvidence: list[EvidenceItem] = field(default_factory=list)


class ChartAgent:
    agent_id = "chart-agent"
    role = "chart-analysis"

    def analyze(self, context: AgentContext) -> AgentFinding:
        chart_document = context.chartContext.get("chartDocument") if isinstance(context.chartContext.get("chartDocument"), dict) else {}
        visible_summary = context.chartContext.get("visibleSummary") if isinstance(context.chartContext.get("visibleSummary"), dict) else {}
        data_status = context.chartContext.get("dataStatus") if isinstance(context.chartContext.get("dataStatus"), dict) else {}
        timeframe = chart_document.get("timeframe") or "unknown"
        last_price = visible_summary.get("lastPrice")
        change = visible_summary.get("change")
        candle_count = data_status.get("candleCount", 0)
        summary = f"{context.symbol} chart context is available for {timeframe} with {candle_count} candles."
        if last_price or change:
            summary = f"{context.symbol} chart shows last price {last_price or 'unknown'} and visible change {change or 'unknown'}."
        evidence = [
            EvidenceItem(
                provider="chart",
                status="available" if candle_count else "partial",
                title="Chart context",
                summary=summary,
                raw={
                    "timeframe": timeframe,
                    "visibleSummary": visible_summary,
                    "dataStatus": data_status,
                },
            )
        ]
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="The chart agent reuses the chart context shape produced for the existing Agent 01 flow.",
            confidence=0.72 if candle_count else 0.42,
            evidence=evidence,
            tags=["chart", "agent-01-compatible"],
        )


class ProviderBackedAgent:
    agent_id = "provider-agent"
    role = "provider"
    provider_name = "provider"

    def __init__(self, provider):
        self.provider = provider

    def analyze(self, context: AgentContext) -> AgentFinding:
        evidence = self.provider.fetch(ProviderRequest(context.symbol, context.intent))
        has_data = any(item.status == "available" for item in evidence)
        if has_data:
            summary = f"{self.provider_name} evidence available for {context.symbol}."
        elif evidence:
            summary = f"{self.provider_name} evidence unavailable for {context.symbol}: {evidence[0].summary}"
        else:
            summary = f"{self.provider_name} evidence unavailable for {context.symbol}."
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="Provider-backed agents expose source availability while the conductor keeps no-data evidence transparent.",
            confidence=0.35 if not has_data else 0.65,
            evidence=evidence,
            tags=[self.provider_name, "provider-adapter"],
        )


class NewsAgent(ProviderBackedAgent):
    agent_id = "news-agent"
    role = "news-analysis"
    provider_name = "news"

    def __init__(self, provider=None):
        super().__init__(provider or ClickHouseNewsProvider())


class MacroAgent(ProviderBackedAgent):
    agent_id = "macro-agent"
    role = "macro-analysis"
    provider_name = "macro"

    def __init__(self, provider=None):
        super().__init__(provider or EmptyMacroProvider())


class OntologyAgent(ProviderBackedAgent):
    agent_id = "ontology-agent"
    role = "company-relationship-analysis"
    provider_name = "ontology"

    def __init__(self, provider=None):
        super().__init__(provider or EmptyOntologyProvider())


class UnusualEventExplainerAgent:
    agent_id = "unusual-event-explainer-agent"
    role = "unusual-event-explanation"

    def analyze(self, context: AgentContext) -> AgentFinding:
        if not context.marketEvents:
            return AgentFinding(
                agentId=self.agent_id,
                role=self.role,
                summary="No unusual market event was supplied.",
                rationale="The explainer only expands detected events and stays quiet when the event detector has no signal.",
                confidence=0.5,
                tags=["event-explainer"],
            )

        event = max(context.marketEvents, key=lambda item: severity_rank(item.severity))
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=f"{event.symbol} has a {event.severity} {event.eventType} event.",
            rationale=event.summary,
            confidence=0.74,
            evidence=event.evidence,
            tags=["event-explainer", event.eventType, event.severity],
        )


class MarketSummaryAgent:
    agent_id = "market-summary-agent"
    role = "market-summary"

    def analyze(self, context: AgentContext, findings: list[AgentFinding]) -> AgentFinding:
        event_count = len(context.marketEvents)
        missing_provider_count = sum(
            1
            for finding in findings
            for evidence in finding.evidence
            if evidence.status == "no-data"
        )
        summary = f"{context.symbol} analysis combined {len(findings)} role findings."
        if event_count:
            summary = f"{context.symbol} analysis found {event_count} unusual event signal(s)."
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale=f"{missing_provider_count} provider evidence source(s) are intentionally empty in v1.",
            confidence=0.6,
            tags=["summary"],
        )


class VerificationGuardrailAgent:
    agent_id = "verification-guardrail-agent"
    role = "verification-guardrail"

    def analyze(self, context: AgentContext, findings: list[AgentFinding]) -> AgentFinding:
        risk_terms = ("buy now", "sell now", "place order", "automatic order", "auto trade")
        combined = " ".join(f"{finding.summary} {finding.rationale}" for finding in findings).lower()
        blocked_terms = [term for term in risk_terms if term.lower() in combined]
        summary = "No trading-action guardrail violation detected."
        confidence = 0.8
        if blocked_terms:
            summary = "Trading-action language was detected and must be removed before display."
            confidence = 0.95
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="The verification agent checks for unsupported order execution language and no-data provider transparency.",
            confidence=confidence,
            tags=["verification", "guardrail", *blocked_terms],
        )


class NotificationDecisionAgent:
    def decide(self, analysis_id: str, context: AgentContext) -> NotificationDecision:
        event = max(context.marketEvents, key=lambda item: severity_rank(item.severity), default=None)
        if not event:
            level = "none"
            title = f"{context.symbol} analysis ready"
            message = "No unusual market alert was detected."
            reason = "No event detector signal was attached to this analysis."
            event_id = None
        else:
            level = event.severity if event.severity in {"info", "watch", "alert", "critical"} else "watch"
            title = f"{event.symbol} {event.eventType.replace('_', ' ')}"
            message = event.summary
            reason = "Notification level follows the strongest attached market event severity."
            event_id = event.eventId

        return NotificationDecision(
            decisionId=stable_id("notification", {"analysisId": analysis_id, "eventId": event_id, "level": level}),
            analysisId=analysis_id,
            eventId=event_id,
            symbol=context.symbol,
            level=level,
            showToast=level in {"watch", "alert", "critical"},
            title=title,
            message=message,
            reason=reason,
            createdAt=utc_now_iso(),
        )


class LayoutAgent:
    def propose(self, context: AgentContext) -> LayoutProposal:
        commands: list[dict[str, Any]] = []
        if context.marketEvents:
            commands.append({
                "type": "layout.panel.add",
                "payload": {
                    "panelType": "notifications",
                    "props": {"symbol": context.symbol},
                },
            })
        return LayoutProposal(
            title="Agent analysis workspace",
            rationale="Show notifications when unusual events are present; leave layout commands in proposal review.",
            commands=commands,
        )


def severity_rank(value: str) -> int:
    order = {"none": 0, "info": 1, "watch": 2, "alert": 3, "critical": 4}
    return order.get(value, 1)
