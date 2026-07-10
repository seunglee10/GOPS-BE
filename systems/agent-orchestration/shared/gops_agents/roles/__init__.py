from __future__ import annotations

import json
import os
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..contracts import AgentFinding, EvidenceItem, LayoutProposal, MarketEvent, NotificationDecision, stable_id, utc_now_iso
from ..intent_understanding.schema import UiTask
from ..orchestration.routing import parse_openai_text_json
from ..orchestration.ui_intent import UIIntent
from ..providers import ClickHouseFinancialProvider, ClickHouseNewsProvider, EmptyMacroProvider, GraphDBOntologyProvider, ProviderRequest
from ..providers.news_localization import NewsLocalizationService


@dataclass
class AgentContext:
    symbol: str
    intent: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    chartContext: dict[str, Any] = field(default_factory=dict)
    layoutContext: dict[str, Any] = field(default_factory=dict)
    references: list[dict[str, Any]] = field(default_factory=list)
    uiContext: dict[str, Any] = field(default_factory=dict)
    operationIR: dict[str, Any] = field(default_factory=dict)
    marketEvents: list[MarketEvent] = field(default_factory=list)
    providerEvidence: list[EvidenceItem] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    runtimeContext: Any | None = None
    newsSymbols: list[str] = field(default_factory=list)
    newsTopic: str | None = None
    relationshipSymbols: list[str] = field(default_factory=list)
    newsDailySummaries: list[dict[str, Any]] = field(default_factory=list)
    intentType: str | None = None
    selectedRoles: list[str] = field(default_factory=list)
    retrievalContext: Any | None = None
    entityResolution: dict[str, Any] = field(default_factory=dict)
    queryUnderstanding: dict[str, Any] = field(default_factory=dict)
    subjectValidation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceGridSpec:
    cols: int = 8
    rows: int = 5


@dataclass
class BandPackResult:
    placements: list[dict[str, Any]]
    unplaceable: list[dict[str, Any]]


@dataclass
class ChartArrangement:
    placements: list[dict[str, Any]]
    unplaceable: list[dict[str, Any]]

    @property
    def valid(self) -> bool:
        return not self.unplaceable


DEFAULT_WORKSPACE_GRID = WorkspaceGridSpec()


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

    def __init__(self, provider=None, localizer=None):
        super().__init__(provider or ClickHouseNewsProvider())
        self.localizer = localizer or NewsLocalizationService()

    def analyze(self, context: AgentContext) -> AgentFinding:
        news_only = is_news_only_context(context)
        started_at = time.perf_counter()
        daily_summaries = []
        try:
            request = ProviderRequest(context.symbol, context.intent, symbols=tuple(context.newsSymbols))
            evidence = self.provider.fetch(request)
            if hasattr(self.provider, "fetch_daily_summaries"):
                daily_summaries = list(self.provider.fetch_daily_summaries(request))
        finally:
            add_context_timing_ms(context, "newsFetchMs", (time.perf_counter() - started_at) * 1000)
        evidence = self.localizer.localize(
            symbol=context.symbol,
            intent=context.intent,
            evidence=evidence,
            allow_runtime_openai=runtime_news_openai_allowed() and not news_only,
        )
        record_news_relevance_counts(context, evidence)
        analysis = analyze_news_evidence(context, evidence)
        openai_analysis = None
        if runtime_role_openai_allowed("news") and not news_only:
            openai_analysis = role_analysis_with_openai(
                role="news",
                context=context,
                evidence=evidence,
                fallback=analysis,
                schema_name="news_agent_analysis",
            )
        analysis = openai_analysis or analysis
        finding = AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=str(analysis["summary"]),
            rationale=str(analysis["rationale"]),
            confidence=float(analysis["confidence"]),
            evidence=evidence,
            tags=[str(item) for item in analysis["tags"]],
        )
        finding.daily_summaries = [item for item in daily_summaries if isinstance(item, dict)]
        return finding


def add_context_timing_ms(context: AgentContext, key: str, elapsed_ms: float) -> None:
    current = context.timing.get(key)
    context.timing[key] = (float(current) if isinstance(current, (int, float)) else 0.0) + elapsed_ms


def add_context_timing_count(context: AgentContext, key: str, count: int = 1) -> None:
    current = context.timing.get(key)
    context.timing[key] = (int(current) if isinstance(current, int) else 0) + count


def is_news_only_context(context: AgentContext) -> bool:
    roles = [str(role) for role in context.selectedRoles]
    return context.intentType == "news" and roles == ["news"]


def runtime_news_openai_allowed() -> bool:
    return str(os.getenv("AGENT_ALLOW_RUNTIME_NEWS_OPENAI") or "").strip().lower() in {"1", "true", "yes", "on"}


def runtime_role_openai_allowed(role: str) -> bool:
    role_key = f"AGENT_{role.upper()}_ROLE_ANALYSIS_PROVIDER"
    return os.getenv(role_key) == "openai" or os.getenv("AGENT_ROLE_ANALYSIS_PROVIDER") == "openai"


def record_news_relevance_counts(context: AgentContext, evidence: list[EvidenceItem]) -> None:
    direct = 0
    mention = 0
    for item in evidence:
        if item.provider != "news" or item.status != "available":
            continue
        raw = item.raw if isinstance(item.raw, dict) else {}
        level = str(raw.get("subjectRelevance") or "").strip().lower()
        if level in {"primary", "secondary"}:
            direct += 1
        elif level == "mention":
            mention += 1
    context.timing["directNewsCount"] = direct
    context.timing["mentionNewsCount"] = mention


def financial_comparison_requested(context: AgentContext) -> bool:
    intent_type = str(context.intentType or "").lower()
    intent_text = str(context.intent or "").lower()
    selected_roles = [str(role).lower() for role in context.selectedRoles]
    comparison_terms = ("compare", "comparison", "peer", "vs", "versus", "경쟁사", "비교", "대비", "동종")
    return (
        "financial-comparison" in intent_type
        or ("financial" in selected_roles and any(term in intent_text for term in comparison_terms))
        or bool(context.relationshipSymbols)
    )


def financial_summary_from_evidence(symbol: str, evidence: list[EvidenceItem]) -> str:
    summary_item = next((item for item in evidence if "peer" not in str(item.title).lower()), evidence[0])
    peer_item = next((item for item in evidence if "peer" in str(item.title).lower()), None)
    if peer_item:
        return f"{summary_item.summary} {peer_item.summary}"
    return summary_item.summary or f"{symbol} SEC 재무 근거를 확인했습니다."


class MacroAgent(ProviderBackedAgent):
    agent_id = "macro-agent"
    role = "macro-analysis"
    provider_name = "macro"

    def __init__(self, provider=None):
        super().__init__(provider or EmptyMacroProvider())


class FinancialAgent(ProviderBackedAgent):
    agent_id = "financial-agent"
    role = "financial-analysis"
    provider_name = "financial"

    def __init__(self, provider=None):
        super().__init__(provider or ClickHouseFinancialProvider())

    def analyze(self, context: AgentContext) -> AgentFinding:
        request = ProviderRequest(context.symbol, context.intent, symbols=tuple(context.relationshipSymbols or context.newsSymbols))
        evidence = self.provider.fetch(request)
        if financial_comparison_requested(context):
            evidence = [*evidence, *self.provider.fetch_peer(request)]
        available = [item for item in evidence if item.provider == "financial" and item.status == "available"]
        no_data = [item for item in evidence if item.provider == "financial" and item.status == "no-data"]
        if available:
            summary = financial_summary_from_evidence(context.symbol, available)
            confidence = 0.68
        else:
            summary = no_data[0].summary if no_data else f"{context.symbol} SEC 재무 근거가 없습니다."
            confidence = 0.28
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="Financial agent uses precomputed SEC fundamentals snapshots only.",
            confidence=confidence,
            evidence=evidence,
            tags=["financial", "sec", "fundamentals"],
        )


class OntologyAgent(ProviderBackedAgent):
    agent_id = "ontology-agent"
    role = "company-relationship-analysis"
    provider_name = "ontology"

    def __init__(self, provider=None):
        super().__init__(provider or GraphDBOntologyProvider())

    def analyze(self, context: AgentContext) -> AgentFinding:
        evidence = self.provider.fetch(ProviderRequest(context.symbol, context.intent, symbols=tuple(context.relationshipSymbols)))
        analysis = analyze_ontology_evidence(context, evidence)
        openai_analysis = None
        if os.getenv("AGENT_ONTOLOGY_ROLE_ANALYSIS_PROVIDER") == "openai":
            openai_analysis = role_analysis_with_openai(
                role="ontology",
                context=context,
                evidence=evidence,
                fallback=analysis,
                schema_name="ontology_agent_analysis",
            )
        analysis = openai_analysis or analysis
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=str(analysis["summary"]),
            rationale=str(analysis["rationale"]),
            confidence=float(analysis["confidence"]),
            evidence=evidence,
            tags=[str(item) for item in analysis["tags"]],
        )


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
        conflicts = detect_cross_agent_conflicts(findings)
        summary = "No trading-action guardrail violation detected."
        confidence = 0.8
        if blocked_terms:
            summary = "Trading-action language was detected and must be removed before display."
            confidence = 0.95
        elif conflicts:
            summary = " ".join(conflicts)
            confidence = 0.86
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="The verification agent checks for unsupported order execution language and no-data provider transparency.",
            confidence=confidence,
            tags=["verification", "guardrail", *blocked_terms, *(["cross-agent-conflict"] if conflicts else [])],
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
    def propose(self, context: AgentContext, route=None) -> LayoutProposal:
        panels = normalize_layout_panels(context.layoutContext)
        if not panels:
            news_panel_props = build_news_panel_props(context.symbol, context.providerEvidence, context.newsDailySummaries)
            return LayoutProposal(
                title="Agent analysis workspace",
                rationale="No layout context was supplied, so the layout agent only proposed display panels with available evidence.",
                commands=[news_panel_add_command(news_panel_props)] if should_add_news_panel_without_layout(news_panel_props) else [],
                panelPriorities=[],
            )

        primary_type = primary_panel_type_for_route(route, context)
        priorities = panel_priorities_for_route(panels, primary_type, route, context)
        commands = priority_commands(priorities)
        news_panel_props = build_news_panel_props(context.symbol, context.providerEvidence, context.newsDailySummaries)
        if news_panel_props:
            commands.append(news_panel_props_command(panels, news_panel_props))
        primary_panel = first_panel_of_type(panels, primary_type)
        if primary_panel and primary_panel["type"] != "orderTicket":
            move_command = primary_panel_move_command(panels, primary_panel)
            if move_command:
                commands.append(move_command)
        if commands:
            commands.append(layout_command("layout.reflow", {"reason": "agent-layout-priority"}))

        return LayoutProposal(
            title="Agent analysis workspace",
            rationale=f"Prioritized {primary_type} for the current user intent and preserved pinned panel boundaries.",
            commands=commands,
            panelPriorities=priorities,
        )


class UIAgent:
    def propose_many(self, context: AgentContext, ui_tasks: list[Any]) -> LayoutProposal:
        tasks = [ui_task_from_payload(item) for item in ui_tasks]
        tasks = [task for task in tasks if task is not None]
        if not tasks:
            return self.propose(context, UIIntent(False, "non-ui", None, None, "unknown", None, None, 0.0, "No UI tasks were supplied."))
        preset_task = next((task for task in tasks if task.action == "load" or task.presetId), None)
        if preset_task is not None:
            return propose_preset_load_layout(preset_task)
        panels = normalize_layout_panels(context.layoutContext)
        chart_tasks = [task for task in tasks if is_chart_task(task)]
        if len(chart_tasks) >= 2:
            return propose_chart_pair_layout(context, panels, chart_tasks)
        if chart_tasks:
            return propose_chart_add_layout(context, panels, chart_tasks[0])
        keep_task = next((task for task in tasks if task.action == "keep"), None)
        if keep_task is not None:
            return propose_keep_only_layout(panels, keep_task)
        if len(tasks) == 1 and not is_multi_ui_task(tasks[0]):
            return self.propose(context, ui_intent_from_task(tasks[0]))

        if any(task.action == "close" for task in tasks):
            return propose_remove_panels_layout(panels, [task for task in tasks if task.action == "close"])

        target_panel_types = multi_ui_target_panel_types(tasks, panels)
        if not target_panel_types:
            return LayoutProposal(
                title="UI layout request",
                rationale="대상 패널을 찾지 못했습니다. 차트, 뉴스, 주문처럼 패널 이름을 포함해 주세요.",
                commands=[],
                autoApply=False,
                panelPriorities=[],
            )

        target_panels, add_panel_types = materialize_target_panels(panels, target_panel_types)
        position_intent = next((task.positionIntent for task in tasks if task.positionIntent), None)
        placements = arrange_panel_set(panels, target_panels, position_intent)
        if not placements:
            return LayoutProposal(
                title="UI layout request",
                rationale="요청한 패널 묶음을 배치할 공간을 찾지 못했습니다.",
                commands=[],
                autoApply=False,
                panelPriorities=multi_ui_panel_priorities(panels, [panel["id"] for panel in target_panels]),
            )

        placement_by_id = {item["panelId"]: item["placement"] for item in placements}
        commands = []
        for panel_type in add_panel_types:
            panel_id = proposed_panel_id(panel_type)
            payload = {
                "panelId": panel_id,
                "panelType": panel_type,
                "placement": placement_by_id.get(panel_id, default_panel_placement(panel_type)),
                "layoutWeight": 100,
            }
            props = panel_props_for_context(context, panel_type)
            if props:
                payload["props"] = props
                if "symbol" in props:
                    payload["symbol"] = props["symbol"]
            commands.append(layout_command(
                "layout.panel.add",
                payload,
                {"panelId": panel_id},
            ))
        for panel in target_panels:
            commands.append(layout_command(
                "layout.panel.priority.set",
                {"panelId": panel["id"], "layoutWeight": 100},
                {"panelId": panel["id"]},
            ))
        commands.append(layout_command(
            "layout.panels.arrange",
            {
                "reason": "ui-agent-multi-panel-layout-intent",
                "placements": placements,
            },
            {"panelIds": [panel["id"] for panel in target_panels]},
        ))
        commands.append(layout_command("layout.reflow", {"reason": "ui-agent-multi-panel-layout-intent"}))
        return LayoutProposal(
            title="UI layout request",
            rationale="요청한 패널들을 다시 배치했습니다.",
            commands=commands,
            autoApply=True,
            panelPriorities=multi_ui_panel_priorities([*panels, *[panel for panel in target_panels if panel["id"] not in {item["id"] for item in panels}]], [panel["id"] for panel in target_panels]),
        )

    def propose(self, context: AgentContext, ui_intent: UIIntent) -> LayoutProposal:
        panels = normalize_layout_panels(context.layoutContext)
        if not ui_intent.isUiIntent:
            return LayoutProposal(
                title="UI layout request",
                rationale="레이아웃 변경 요청으로 확정하지 못했습니다.",
                commands=[],
                autoApply=False,
                panelPriorities=[],
            )

        task = ui_task_from_intent(ui_intent)
        if task is not None and is_chart_task(task):
            return propose_chart_add_layout(context, panels, task)

        target_panel = resolve_ui_target_panel(panels, ui_intent)
        if not target_panel:
            restore_proposal = propose_missing_panel_layout(context, panels, ui_intent)
            if restore_proposal is not None:
                return restore_proposal
            add_command = ui_add_command(ui_intent)
            return LayoutProposal(
                title="UI layout request",
                rationale=(
                    f"{default_panel_title(ui_intent.targetPanelType or '')} 패널을 열었습니다."
                    if add_command
                    else "대상 패널을 찾지 못했습니다. 차트, 뉴스, 주문처럼 패널 이름을 포함해 주세요."
                ),
                commands=[add_command] if add_command else [],
                autoApply=bool(add_command),
                panelPriorities=[],
            )

        if target_panel.get("layoutPinned"):
            return LayoutProposal(
                title="UI layout request",
                rationale=f"{target_panel.get('title') or target_panel['id']} 패널은 고정되어 있어 옮기지 않았습니다.",
                commands=[],
                autoApply=False,
                panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
            )

        if ui_intent.action == "close":
            remaining_panels = [panel for panel in panels if panel["id"] != target_panel["id"]]
            return LayoutProposal(
                title="UI layout request",
                rationale=f"{target_panel.get('title') or target_panel['id']} 패널을 숨겼습니다.",
                commands=[
                    layout_command(
                        "layout.panel.remove",
                        {"panelId": target_panel["id"]},
                        {"panelId": target_panel["id"]},
                    ),
                    layout_command("layout.reflow", {"reason": "ui-agent-panel-remove"}),
                ],
                autoApply=True,
                panelPriorities=multi_ui_panel_priorities(remaining_panels, []),
            )

        placements = arrange_ui_panels(panels, target_panel, ui_intent)
        if not placements:
            return LayoutProposal(
                title="UI layout request",
                rationale="요청한 배치로 옮길 수 있는 공간을 찾지 못했습니다.",
                commands=[],
                autoApply=False,
                panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
            )

        commands = []
        props = panel_props_for_context(context, target_panel["type"])
        if props:
            commands.append(layout_command(
                "layout.panel.props.update",
                {"panelId": target_panel["id"], "props": props},
                {"panelId": target_panel["id"]},
            ))
        commands.extend([
            layout_command(
                "layout.panel.priority.set",
                {"panelId": target_panel["id"], "layoutWeight": 100},
                {"panelId": target_panel["id"]},
            ),
            layout_command(
                "layout.panels.arrange",
                {
                    "reason": "ui-agent-layout-intent",
                    "placements": placements,
                },
                {"panelId": target_panel["id"]},
            ),
            layout_command(
                "layout.panel.move",
                {"panelId": target_panel["id"], "placement": placements[0]["placement"]},
                {"panelId": target_panel["id"]},
            ),
            layout_command("layout.reflow", {"reason": "ui-agent-layout-intent"}),
        ])
        return LayoutProposal(
            title="UI layout request",
            rationale=panel_action_message(target_panel, ui_intent.action),
            commands=commands,
            autoApply=True,
            panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
        )


DEFAULT_WORKSPACE_PANEL_TYPES = ["chart", "newsFeed", "aiSummary"]


def ui_task_from_payload(value: Any) -> UiTask | None:
    if isinstance(value, UiTask):
        return value
    if not isinstance(value, dict):
        return None
    return UiTask(
        action=str(value.get("action") or "focus"),
        targetPanelType=optional_text(value.get("targetPanelType")),
        targetPanelId=optional_text(value.get("targetPanelId")),
        targetPanelTypes=[str(item) for item in value.get("targetPanelTypes", []) if isinstance(item, str)] if isinstance(value.get("targetPanelTypes"), list) else [],
        targetPanelIds=[str(item) for item in value.get("targetPanelIds", []) if isinstance(item, str)] if isinstance(value.get("targetPanelIds"), list) else [],
        layoutPreset=optional_text(value.get("layoutPreset")),
        presetId=optional_text(value.get("presetId")),
        presetName=optional_text(value.get("presetName")),
        presetKind=optional_text(value.get("presetKind")),
        sizeIntent=optional_text(value.get("sizeIntent")),
        positionIntent=optional_text(value.get("positionIntent")),
        chartAction=optional_text(value.get("chartAction")),
        symbol=optional_text(value.get("symbol")),
        confidence=read_float(value.get("confidence"), 0.7),
        source=str(value.get("source") or "ui-fallback"),
        reason=str(value.get("reason") or "UI task from query understanding."),
    )


def ui_task_from_intent(ui_intent: UIIntent) -> UiTask | None:
    if not ui_intent.isUiIntent:
        return None
    return UiTask(
        action=ui_intent.action or "focus",
        targetPanelType=ui_intent.targetPanelType,
        targetPanelId=ui_intent.targetPanelId,
        sizeIntent=ui_intent.sizeIntent,
        positionIntent=ui_intent.positionIntent,
        confidence=ui_intent.confidence,
        source=ui_intent.source or "ui-intent",
        reason=ui_intent.reason or "UI intent converted to a UI task.",
    )


def optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def is_multi_ui_task(task: UiTask) -> bool:
    return bool(task.action in {"keep", "load"} or task.chartAction == "add" or task.layoutPreset or task.presetId or len(task.targetPanelTypes) > 1 or len(task.targetPanelIds) > 1)


def is_chart_task(task: UiTask) -> bool:
    multi_target = task.layoutPreset or len(task.targetPanelTypes) > 1 or len(task.targetPanelIds) > 1
    return not multi_target and task.action in {"open", "focus"} and task.targetPanelType == "chart"


def propose_preset_load_layout(task: UiTask) -> LayoutProposal:
    preset_id = optional_text(task.presetId)
    preset_name = optional_text(task.presetName) or "요청한"
    if not preset_id:
        return LayoutProposal(
            title="UI preset request",
            rationale="어떤 프리셋을 열지 확정하지 못했습니다. 프리셋 이름을 조금 더 구체적으로 말해 주세요.",
            commands=[],
            autoApply=False,
            panelPriorities=[],
        )
    payload: dict[str, Any] = {
        "presetId": preset_id,
        "presetName": preset_name,
    }
    if task.presetKind:
        payload["presetKind"] = task.presetKind
    return LayoutProposal(
        title="UI preset request",
        rationale=f"{preset_name} 프리셋을 열었습니다.",
        commands=[layout_command("layout.load", payload)],
        autoApply=True,
        panelPriorities=[],
    )


def propose_remove_panels_layout(panels: list[dict[str, Any]], tasks: list[UiTask]) -> LayoutProposal:
    remove_ids: list[str] = []
    remove_types: list[str] = []
    for task in tasks:
        remove_ids.extend(task.targetPanelIds)
        remove_types.extend(task.targetPanelTypes or ([task.targetPanelType] if task.targetPanelType else []))
    remove_id_set = set(remove_ids)
    remove_type_set = set(remove_types)
    remove_panels = [
        panel
        for panel in panels
        if panel["id"] in remove_id_set or panel["type"] in remove_type_set
    ]
    if not remove_panels:
        return LayoutProposal(
            title="UI layout request",
            rationale="숨길 패널을 찾지 못했습니다. 차트, 뉴스, 주문처럼 패널 이름을 포함해 주세요.",
            commands=[],
            autoApply=False,
            panelPriorities=multi_ui_panel_priorities(panels, []),
        )

    commands = [
        layout_command(
            "layout.panel.remove",
            {"panelId": panel["id"]},
            {"panelId": panel["id"]},
        )
        for panel in remove_panels
    ]
    commands.append(layout_command("layout.reflow", {"reason": "ui-agent-panel-remove"}))
    remove_label = panel_set_label(remove_panels)
    remaining_ids = [panel["id"] for panel in panels if panel["id"] not in {item["id"] for item in remove_panels}]
    return LayoutProposal(
        title="UI layout request",
        rationale=f"{remove_label} 패널을 숨겼습니다.",
        commands=commands,
        autoApply=True,
        panelPriorities=multi_ui_panel_priorities(panels, remaining_ids),
    )


def propose_keep_only_layout(panels: list[dict[str, Any]], task: UiTask) -> LayoutProposal:
    keep_panel_ids = set(task.targetPanelIds)
    keep_panel_types = set(task.targetPanelTypes or ([task.targetPanelType] if task.targetPanelType else []))
    keep_panels = [
        panel
        for panel in panels
        if panel["id"] in keep_panel_ids or panel["type"] in keep_panel_types
    ]
    if not keep_panels:
        return LayoutProposal(
            title="UI layout request",
            rationale="남길 패널을 찾지 못했습니다. 차트, 뉴스, 주문처럼 패널 이름을 포함해 주세요.",
            commands=[],
            autoApply=False,
            panelPriorities=[],
        )

    keep_ids = {panel["id"] for panel in keep_panels}
    remove_panels = [panel for panel in panels if panel["id"] not in keep_ids]
    if not remove_panels:
        keep_label = panel_set_label(keep_panels)
        return LayoutProposal(
            title="UI layout request",
            rationale=f"이미 {keep_label}만 표시되어 있습니다.",
            commands=[],
            autoApply=False,
            panelPriorities=multi_ui_panel_priorities(panels, list(keep_ids)),
        )

    commands = [
        layout_command(
            "layout.panel.remove",
            {"panelId": panel["id"]},
            {"panelId": panel["id"]},
        )
        for panel in remove_panels
    ]
    commands.append(layout_command("layout.reflow", {"reason": "ui-agent-keep-only"}))
    keep_label = panel_set_label(keep_panels)
    return LayoutProposal(
        title="UI layout request",
        rationale=f"{keep_label}만 남기고 {len(remove_panels)}개 패널을 숨겼습니다.",
        commands=commands,
        autoApply=True,
        panelPriorities=multi_ui_panel_priorities(panels, list(keep_ids)),
    )


def panel_set_label(panels: list[dict[str, Any]]) -> str:
    labels = [str(panel.get("title") or default_panel_title(panel["type"])) for panel in panels]
    unique = []
    for label in labels:
        if label and label not in unique:
            unique.append(label)
    if not unique:
        return "선택한 패널"
    if len(unique) == 1:
        return unique[0]
    return ", ".join(unique)


def panel_action_message(panel: dict[str, Any], action: str) -> str:
    label = str(panel.get("title") or default_panel_title(str(panel.get("type") or "")) or panel.get("id") or "선택한")
    if action == "resize":
        return f"{label} 패널 크기를 조정했습니다."
    if action == "move":
        return f"{label} 패널 위치를 옮겼습니다."
    if action == "open":
        return f"{label} 패널을 열었습니다."
    if action == "focus":
        return f"{label} 패널을 앞으로 배치했습니다."
    return f"{label} 패널을 정리했습니다."


def propose_missing_panel_layout(context: AgentContext, panels: list[dict[str, Any]], ui_intent: UIIntent) -> LayoutProposal | None:
    if not ui_intent.targetPanelType or not is_visibility_panel_action(ui_intent.action):
        return None

    panel_type = ui_intent.targetPanelType
    panel_id = proposed_panel_id(panel_type)
    grid = grid_for_panels(panels)
    target_panel = {
        "id": panel_id,
        "type": panel_type,
        "title": default_panel_title(panel_type),
        "placement": default_panel_placement(panel_type),
        "layoutPinned": False,
        "layoutWeight": 100,
        "minSpan": default_min_span(panel_type),
        "maxSpan": {"colSpan": grid.cols, "rowSpan": grid.rows},
        "_grid": grid,
    }
    placements = arrange_panel_set(panels, [target_panel], ui_intent.positionIntent)
    placement_by_id = {item["panelId"]: item["placement"] for item in placements or []}
    payload = {
        "panelId": panel_id,
        "panelType": panel_type,
        "placement": placement_by_id.get(panel_id, target_panel["placement"]),
        "layoutWeight": 100,
    }
    props = panel_props_for_context(context, panel_type)
    if props:
        payload["props"] = props
        if "symbol" in props:
            payload["symbol"] = props["symbol"]
    commands = [
        layout_command("layout.panel.add", payload, {"panelId": panel_id}),
        layout_command(
            "layout.panel.priority.set",
            {"panelId": panel_id, "layoutWeight": 100},
            {"panelId": panel_id},
        ),
    ]
    if placements:
        commands.append(layout_command(
            "layout.panels.arrange",
            {
                "reason": "ui-agent-panel-restore",
                "placements": placements,
            },
            {"panelId": panel_id},
        ))
    commands.append(layout_command("layout.reflow", {"reason": "ui-agent-panel-restore"}))
    return LayoutProposal(
        title="UI layout request",
        rationale=f"{default_panel_title(panel_type)} 패널을 열었습니다.",
        commands=commands,
        autoApply=True,
        panelPriorities=multi_ui_panel_priorities([*panels, target_panel], [panel_id]),
    )


def is_visibility_panel_action(action: str) -> bool:
    return action in {"open", "focus", "resize", "move", "arrange"}


def panel_props_for_context(context: AgentContext, panel_type: str) -> dict[str, Any]:
    symbol = str(context.symbol or "").strip().upper()
    query_understanding = context.queryUnderstanding if isinstance(context.queryUnderstanding, dict) else {}
    symbol_source = str(query_understanding.get("resolvedSymbolSource") or "").strip()
    if symbol_source not in {"query_company", "chart_shortcut"}:
        return {}
    if symbol and symbol != "UNKNOWN" and panel_type in {"chart", "newsFeed", "ontologyGraph", "orderTicket"}:
        return {"symbol": symbol}
    return {}


def propose_chart_add_layout(context: AgentContext, panels: list[dict[str, Any]], task: UiTask) -> LayoutProposal:
    symbol = chart_task_symbol(context, task)
    if not symbol:
        return LayoutProposal(
            title="UI layout request",
            rationale="추가할 차트 종목을 확정하지 못했습니다.",
            commands=[],
            autoApply=False,
            panelPriorities=[],
        )

    is_add_intent = task.chartAction == "add"
    existing_same_symbol = first_chart_panel_for_symbol(panels, symbol)
    if existing_same_symbol:
        return LayoutProposal(
            title="UI layout request",
            rationale=f"{symbol} 차트가 이미 열려 있어 기존 차트 패널을 앞으로 배치했습니다.",
            commands=[
                layout_command(
                    "layout.panel.priority.set",
                    {"panelId": existing_same_symbol["id"], "layoutWeight": 120},
                    {"panelId": existing_same_symbol["id"]},
                ),
                layout_command("layout.reflow", {"reason": "chart-add-existing-symbol"}),
            ],
            autoApply=True,
            panelPriorities=chart_add_panel_priorities(panels, existing_same_symbol["id"], None),
        )

    chart_panels = [panel for panel in panels if panel["type"] == "chart"]
    anchor_chart = chart_panels[0] if chart_panels else None
    if len(chart_panels) == 1 and not is_add_intent:
        replace_panel = chart_panels[0]
        updated_panels = [
            {**panel, "symbol": symbol, "layoutWeight": 120}
            if panel["id"] == replace_panel["id"]
            else panel
            for panel in panels
        ]
        return LayoutProposal(
            title="UI layout request",
            rationale=f"{symbol} 차트를 기존 차트 패널에 표시했습니다.",
            commands=[
                layout_command(
                    "layout.panel.props.update",
                    {
                        "panelId": replace_panel["id"],
                        "props": {"symbol": symbol},
                        "layoutWeight": 120,
                    },
                    {"panelId": replace_panel["id"]},
                ),
                layout_command(
                    "layout.panel.priority.set",
                    {"panelId": replace_panel["id"], "layoutWeight": 120},
                    {"panelId": replace_panel["id"]},
                ),
                layout_command("layout.reflow", {"reason": "chart-replace-existing-panel"}),
            ],
            autoApply=True,
            panelPriorities=chart_add_panel_priorities(updated_panels, replace_panel["id"], None),
        )

    if len(chart_panels) >= 2:
        replace_panel = chart_panel_to_replace(chart_panels, anchor_chart["id"] if anchor_chart else None)
        updated_panels = [
            {**panel, "symbol": symbol, "layoutWeight": 120}
            if panel["id"] == replace_panel["id"]
            else panel
            for panel in panels
        ]
        anchor = next((panel for panel in updated_panels if panel["type"] == "chart" and panel["id"] != replace_panel["id"]), None)
        arrangement = arrange_chart_comparison(updated_panels, anchor, next(panel for panel in updated_panels if panel["id"] == replace_panel["id"]), task.positionIntent)
        if not arrangement.valid:
            return chart_placement_pick_proposal(context, updated_panels, task, symbol, replace_panel["id"], "차트 비교 배치를 적용할 빈 공간을 찾지 못했습니다.")
        return LayoutProposal(
            title="UI layout request",
            rationale=f"{symbol} 차트를 기존 비교 차트 패널에 표시했습니다.",
            commands=[
                layout_command(
                    "layout.panel.props.update",
                    {
                        "panelId": replace_panel["id"],
                        "props": {"symbol": symbol},
                        "layoutWeight": 120,
                    },
                    {"panelId": replace_panel["id"]},
                ),
                layout_command(
                    "layout.panel.priority.set",
                    {"panelId": replace_panel["id"], "layoutWeight": 120},
                    {"panelId": replace_panel["id"]},
                ),
                layout_command(
                    "layout.panels.arrange",
                    {"reason": "chart-add-priority-reflow", "placements": arrangement.placements},
                    {"panelIds": [item["panelId"] for item in arrangement.placements]},
                ),
                layout_command("layout.reflow", {"reason": "chart-add-priority-reflow"}),
            ],
            autoApply=True,
            panelPriorities=chart_add_panel_priorities(updated_panels, replace_panel["id"], anchor["id"] if anchor else None),
        )

    panel_id = proposed_chart_panel_id(symbol, panels)
    new_panel = {
        "id": panel_id,
        "type": "chart",
        "title": default_panel_title("chart"),
        "symbol": symbol,
        "placement": default_chart_add_placement(task.positionIntent),
        "layoutPinned": False,
        "layoutWeight": 120,
        "minSpan": {"colSpan": 2, "rowSpan": 2},
        "maxSpan": default_max_span("chart"),
    }
    combined_panels = [*panels, new_panel]
    arrangement = arrange_chart_comparison(combined_panels, anchor_chart if is_add_intent else None, new_panel, task.positionIntent)
    if not arrangement.valid:
        return chart_placement_pick_proposal(context, combined_panels, task, symbol, panel_id, "하단 차트 영역에 바로 배치할 수 없어 배치 후보를 준비했습니다.")
    placement_by_id = {item["panelId"]: item["placement"] for item in arrangement.placements}
    return LayoutProposal(
        title="UI layout request",
        rationale=f"{symbol} 차트 패널을 추가했습니다.",
        commands=[
            layout_command(
                "layout.panel.add",
                {
                    "panelId": panel_id,
                    "panelType": "chart",
                    "props": {"symbol": symbol},
                    "symbol": symbol,
                    "layoutWeight": 120,
                    "placement": placement_by_id.get(panel_id, new_panel["placement"]),
                },
                {"panelId": panel_id},
            ),
            layout_command(
                "layout.panel.priority.set",
                {"panelId": panel_id, "layoutWeight": 120},
                {"panelId": panel_id},
            ),
            layout_command(
                "layout.panels.arrange",
                {"reason": "chart-add-priority-reflow", "placements": arrangement.placements},
                {"panelIds": [item["panelId"] for item in arrangement.placements]},
            ),
            layout_command("layout.reflow", {"reason": "chart-add-priority-reflow"}),
        ],
        autoApply=True,
        panelPriorities=chart_add_panel_priorities(combined_panels, panel_id, anchor_chart["id"] if anchor_chart else None),
    )


def propose_chart_pair_layout(context: AgentContext, panels: list[dict[str, Any]], tasks: list[UiTask]) -> LayoutProposal:
    symbols = unique_texts([
        symbol
        for symbol in [chart_task_symbol(context, task) for task in tasks]
        if symbol
    ])
    if len(symbols) < 2:
        return propose_chart_add_layout(context, panels, tasks[0])

    chart_panels = [panel for panel in panels if panel["type"] == "chart"]
    updates: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    working_panels = list(panels)
    chart_a: dict[str, Any] | None = None
    chart_b: dict[str, Any] | None = None

    if chart_panels:
        chart_a = {**chart_panels[0], "symbol": symbols[0], "layoutWeight": 110}
        updates.append({"panelId": chart_panels[0]["id"], "symbol": symbols[0], "layoutWeight": 110})
        working_panels = [chart_a if panel["id"] == chart_panels[0]["id"] else panel for panel in working_panels]
    else:
        chart_a = new_chart_panel(symbols[0], working_panels, layout_weight=110)
        additions.append(chart_a)
        working_panels = [*working_panels, chart_a]

    if len(chart_panels) >= 2:
        second = chart_panels[1]
        chart_b = {**second, "symbol": symbols[1], "layoutWeight": 120}
        updates.append({"panelId": second["id"], "symbol": symbols[1], "layoutWeight": 120})
        working_panels = [chart_b if panel["id"] == second["id"] else panel for panel in working_panels]
    else:
        chart_b = new_chart_panel(symbols[1], working_panels, layout_weight=120)
        additions.append(chart_b)
        working_panels = [*working_panels, chart_b]

    arrangement = arrange_chart_comparison(working_panels, chart_a, chart_b, tasks[0].positionIntent)
    if not arrangement.valid:
        return LayoutProposal(
            title="UI layout request",
            rationale="두 차트를 동시에 배치할 공간을 찾지 못했습니다.",
            commands=[],
            autoApply=False,
            panelPriorities=chart_add_panel_priorities(working_panels, chart_b["id"], chart_a["id"]),
        )

    placement_by_id = {item["panelId"]: item["placement"] for item in arrangement.placements}
    commands: list[dict[str, Any]] = []
    for item in additions:
        commands.append(layout_command(
            "layout.panel.add",
            {
                "panelId": item["id"],
                "panelType": "chart",
                "props": {"symbol": item["symbol"]},
                "symbol": item["symbol"],
                "layoutWeight": item["layoutWeight"],
                "placement": placement_by_id.get(item["id"], item["placement"]),
            },
            {"panelId": item["id"]},
        ))
    for item in updates:
        commands.append(layout_command(
            "layout.panel.props.update",
            {
                "panelId": item["panelId"],
                "props": {"symbol": item["symbol"]},
                "layoutWeight": item["layoutWeight"],
            },
            {"panelId": item["panelId"]},
        ))
    for panel in (chart_a, chart_b):
        commands.append(layout_command(
            "layout.panel.priority.set",
            {"panelId": panel["id"], "layoutWeight": panel["layoutWeight"]},
            {"panelId": panel["id"]},
        ))
    commands.append(layout_command(
        "layout.panels.arrange",
        {"reason": "chart-pair-priority-reflow", "placements": arrangement.placements},
        {"panelIds": [item["panelId"] for item in arrangement.placements]},
    ))
    commands.append(layout_command("layout.reflow", {"reason": "chart-pair-priority-reflow"}))
    extra_notice = " 처음 두 종목만 배치했습니다." if len(symbols) > 2 else ""
    return LayoutProposal(
        title="UI layout request",
        rationale=f"{symbols[0]}와 {symbols[1]} 차트를 같이 표시했습니다.{extra_notice}",
        commands=commands,
        autoApply=True,
        panelPriorities=chart_add_panel_priorities(working_panels, chart_b["id"], chart_a["id"]),
    )


def new_chart_panel(symbol: str, panels: list[dict[str, Any]], *, layout_weight: int) -> dict[str, Any]:
    panel_id = proposed_chart_panel_id(symbol, panels)
    return {
        "id": panel_id,
        "type": "chart",
        "title": default_panel_title("chart"),
        "symbol": symbol,
        "placement": default_chart_add_placement(None),
        "layoutPinned": False,
        "layoutWeight": layout_weight,
        "minSpan": {"colSpan": 2, "rowSpan": 2},
        "maxSpan": default_max_span("chart"),
        "_grid": grid_for_panels(panels),
    }


def chart_task_symbol(context: AgentContext, task: UiTask) -> str:
    symbol = str(task.symbol or context.symbol or "").strip().upper()
    return "" if symbol == "UNKNOWN" else symbol


def first_chart_panel_for_symbol(panels: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    normalized = symbol.upper()
    return next((panel for panel in panels if panel["type"] == "chart" and chart_panel_symbol(panel) == normalized), None)


def chart_panel_symbol(panel: dict[str, Any]) -> str:
    return str(panel.get("symbol") or "").strip().upper()


def chart_panel_to_replace(chart_panels: list[dict[str, Any]], anchor_panel_id: str | None) -> dict[str, Any]:
    replaceable = [
        panel
        for panel in chart_panels
        if not panel.get("layoutPinned") and panel["id"] != anchor_panel_id
    ]
    return sorted(replaceable or chart_panels, key=lambda panel: (read_float(panel.get("layoutWeight"), 0.0), panel["id"]))[0]


def proposed_chart_panel_id(symbol: str, panels: list[dict[str, Any]]) -> str:
    base = f"panel-chart-{symbol.lower().replace('.', '-')}"
    existing = {panel["id"] for panel in panels}
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def default_chart_add_placement(position_intent: str | None) -> dict[str, Any]:
    grid = DEFAULT_WORKSPACE_GRID
    if position_intent == "top":
        return workspace_placement(1, 1, grid.cols, 2, grid)
    if position_intent == "left":
        return workspace_placement(1, 1, grid.cols // 2, grid.rows, grid)
    if position_intent in {"right", "center"}:
        return workspace_placement(grid.cols // 2 + 1, 1, grid.cols // 2, grid.rows, grid)
    return workspace_placement(1, 4, grid.cols, 2, grid)


def arrange_chart_comparison(
    panels: list[dict[str, Any]],
    anchor_chart: dict[str, Any] | None,
    target_chart: dict[str, Any],
    position_intent: str | None,
) -> ChartArrangement:
    grid = grid_for_panels(panels)
    placements: list[dict[str, Any]] = pinned_panel_placements(panels)
    support = [panel for panel in panels if panel["type"] != "chart" and not panel.get("layoutPinned")]
    unplaceable: list[dict[str, Any]] = []

    anchor_id = anchor_chart["id"] if anchor_chart else None
    target_id = target_chart["id"]
    if anchor_chart:
        if support:
            support_pack = pack_support_band(support, 1, 1, grid)
            placements.extend(support_pack.placements)
            unplaceable.extend(support_pack.unplaceable)
            top_row = 2
            top_rows = 2
        else:
            top_row = 1
            top_rows = 3
        first_chart_id = target_id if position_intent == "top" else anchor_id
        second_chart_id = anchor_id if position_intent == "top" else target_id
        placements.extend([
            {
                "panelId": first_chart_id,
                "placement": workspace_placement(1, top_row, grid.cols, top_rows, grid),
                "layoutWeight": 120 if first_chart_id == target_id else chart_anchor_weight(anchor_chart),
            },
            {
                "panelId": second_chart_id,
                "placement": workspace_placement(1, 4, grid.cols, 2, grid),
                "layoutWeight": 120 if second_chart_id == target_id else chart_anchor_weight(anchor_chart),
            },
        ])
    else:
        if support:
            support_pack = pack_support_band(support, 1, 3, grid)
            placements.extend(support_pack.placements)
            unplaceable.extend(support_pack.unplaceable)
            placements.append({"panelId": target_id, "placement": workspace_placement(1, 4, grid.cols, 2, grid), "layoutWeight": 120})
        else:
            placements.append({"panelId": target_id, "placement": workspace_placement(1, 1, grid.cols, grid.rows, grid), "layoutWeight": 120})

    if unplaceable or layout_has_gaps_or_overlaps(placements, grid):
        return ChartArrangement(placements, [*unplaceable, *layout_validation_failures(placements, grid)])
    return ChartArrangement(placements, [])


def chart_placement_pick_proposal(
    context: AgentContext,
    panels: list[dict[str, Any]],
    task: UiTask,
    symbol: str,
    panel_id: str,
    reason: str,
) -> LayoutProposal:
    candidates = chart_placement_candidates(panels, symbol, panel_id)
    return LayoutProposal(
        title="UI layout request",
        rationale=reason,
        commands=[
            layout_command(
                "layout.placement.pick",
                {
                    "panelType": "chart",
                    "panelId": panel_id,
                    "symbol": symbol,
                    "positionIntent": task.positionIntent,
                    "candidates": candidates,
                },
                {"panelId": panel_id},
            )
        ] if candidates else [],
        autoApply=False,
        panelPriorities=chart_add_panel_priorities(panels, panel_id, None),
    )


def chart_placement_candidates(panels: list[dict[str, Any]], symbol: str, panel_id: str) -> list[dict[str, Any]]:
    grid = grid_for_panels(panels)
    candidate_rows = [
        ("bottom", "맨 아래", 4),
        ("top", "맨 위", 1),
        ("middle", "가운데", 2),
        ("upper", "위쪽", 3),
    ]
    candidates = []
    chart_panel = next((panel for panel in panels if panel["id"] == panel_id), None) or new_chart_panel(symbol, panels, layout_weight=120)
    for candidate_id, label, row in candidate_rows:
        placement = workspace_placement(1, row, grid.cols, 2, grid)
        arrangement = build_chart_candidate_arrangement(panels, {**chart_panel, "placement": placement}, grid)
        if not arrangement.valid:
            continue
        candidates.append({
            "id": candidate_id,
            "label": label,
            "placement": placement,
            "arrangement": arrangement.placements,
        })
        if len(candidates) >= 3:
            break
    if not candidates:
        full_placement = workspace_placement(1, 1, grid.cols, grid.rows, grid)
        arrangement = build_chart_candidate_arrangement(panels, {**chart_panel, "placement": full_placement}, grid)
        if arrangement.valid:
            candidates.append({
                "id": "full",
                "label": "전체 영역",
                "placement": full_placement,
                "arrangement": arrangement.placements,
            })
    return candidates


def build_chart_candidate_arrangement(
    panels: list[dict[str, Any]],
    chart_panel: dict[str, Any],
    grid: WorkspaceGridSpec,
) -> ChartArrangement:
    placements = pinned_panel_placements(panels)
    chart_placement = chart_panel["placement"]
    if any(overlaps(chart_placement, item["placement"]) for item in placements):
        return ChartArrangement(placements, [{"panelId": chart_panel["id"], "reason": "chart-overlaps-pinned"}])
    placements.append({"panelId": chart_panel["id"], "placement": chart_placement, "layoutWeight": 120})
    occupied = occupied_cells([item["placement"] for item in placements], grid)
    partial_blocked_rows = [
        row
        for row in range(1, grid.rows + 1)
        if 0 < len([col for col in range(1, grid.cols + 1) if (col, row) in occupied]) < grid.cols
    ]
    if partial_blocked_rows:
        return ChartArrangement(placements, [{"panelId": chart_panel["id"], "reason": "partial-row-blocked"}])

    support = [panel for panel in panels if panel["type"] != "chart" and not panel.get("layoutPinned")]
    free_rows = [
        row
        for row in range(1, grid.rows + 1)
        if all((col, row) not in occupied for col in range(1, grid.cols + 1))
    ]
    if support:
        bands = contiguous_row_bands(free_rows)
        if not bands:
            return ChartArrangement(placements, [{"panelId": panel["id"], "reason": "no-support-band"} for panel in support])
        start_row, row_span = max(bands, key=lambda band: (band[1], -band[0]))
        support_pack = pack_support_band(support, start_row, row_span, grid)
        placements.extend(support_pack.placements)
        if support_pack.unplaceable:
            return ChartArrangement(placements, support_pack.unplaceable)
    if layout_has_gaps_or_overlaps(placements, grid):
        return ChartArrangement(placements, layout_validation_failures(placements, grid))
    return ChartArrangement(placements, [])


def contiguous_row_bands(rows: list[int]) -> list[tuple[int, int]]:
    if not rows:
        return []
    bands: list[tuple[int, int]] = []
    start = rows[0]
    previous = rows[0]
    for row in rows[1:]:
        if row == previous + 1:
            previous = row
            continue
        bands.append((start, previous - start + 1))
        start = row
        previous = row
    bands.append((start, previous - start + 1))
    return bands


def pack_support_band(
    panels: list[dict[str, Any]],
    start_row: int,
    row_span: int,
    grid: WorkspaceGridSpec,
) -> BandPackResult:
    ordered = sorted(panels, key=supporting_panel_sort_key)
    if not ordered:
        return BandPackResult([], [])
    if row_span <= 0:
        return BandPackResult([], [{"panelId": panel["id"], "reason": "no-band"} for panel in ordered])

    rows_needed = max(1, min(row_span, (len(ordered) + grid.cols - 1) // grid.cols))
    if len(ordered) > grid.cols * rows_needed:
        return BandPackResult([], [{"panelId": panel["id"], "reason": "support-band-full"} for panel in ordered[grid.cols * rows_needed:]])

    placements: list[dict[str, Any]] = []
    unplaceable: list[dict[str, Any]] = []
    cursor = 0
    row_heights = distribute_units(row_span, rows_needed)
    for row_index, row_height in enumerate(row_heights):
        remaining = len(ordered) - cursor
        rows_left = rows_needed - row_index
        count = min(grid.cols, max(1, remaining - grid.cols * (rows_left - 1)))
        widths = distribute_units(grid.cols, count)
        col = 1
        for width in widths:
            panel = ordered[cursor]
            min_span = panel.get("minSpan") if isinstance(panel.get("minSpan"), dict) else default_min_span(panel["type"])
            min_cols = read_int(min_span.get("colSpan"), 1)
            min_rows = read_int(min_span.get("rowSpan"), 1)
            if width < min_cols or row_height < min_rows:
                unplaceable.append({"panelId": panel["id"], "reason": "min-span-too-large"})
            else:
                placements.append({
                    "panelId": panel["id"],
                    "placement": workspace_placement(col, start_row + sum(row_heights[:row_index]), width, row_height, grid),
                    "layoutWeight": max(20, min(60, int(read_float(panel.get("layoutWeight"), 35.0)))),
                })
            col += width
            cursor += 1
    return BandPackResult(placements, unplaceable)


def distribute_units(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base = total // count
    remainder = total % count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def pinned_panel_placements(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "panelId": panel["id"],
            "placement": panel["placement"],
            "layoutWeight": max(20, min(80, int(read_float(panel.get("layoutWeight"), 50.0)))),
        }
        for panel in panels
        if panel.get("layoutPinned")
    ]


def layout_has_gaps_or_overlaps(placements: list[dict[str, Any]], grid: WorkspaceGridSpec) -> bool:
    counts = occupied_cell_counts([item["placement"] for item in placements], grid)
    if any(count > 1 for count in counts.values()):
        return True
    return len(counts) != grid.cols * grid.rows


def layout_validation_failures(placements: list[dict[str, Any]], grid: WorkspaceGridSpec) -> list[dict[str, Any]]:
    counts = occupied_cell_counts([item["placement"] for item in placements], grid)
    failures: list[dict[str, Any]] = []
    if any(count > 1 for count in counts.values()):
        failures.append({"reason": "overlap"})
    missing = grid.cols * grid.rows - len(counts)
    if missing:
        failures.append({"reason": "gap", "cells": missing})
    for item in placements:
        if not placement_within_grid(item["placement"], grid):
            failures.append({"panelId": item["panelId"], "reason": "out-of-grid"})
    return failures


def occupied_cell_counts(placements: list[dict[str, Any]], grid: WorkspaceGridSpec) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    for placement in placements:
        for cell in cells_for_placement(placement, grid):
            counts[cell] = counts.get(cell, 0) + 1
    return counts


def placement_within_grid(placement: dict[str, Any], grid: WorkspaceGridSpec) -> bool:
    if placement.get("group") != "workspace":
        return True
    col = read_int(placement.get("col"), 1)
    row = read_int(placement.get("row"), 1)
    col_span = read_int(placement.get("colSpan"), 1)
    row_span = read_int(placement.get("rowSpan"), 1)
    return col >= 1 and row >= 1 and col + col_span - 1 <= grid.cols and row + row_span - 1 <= grid.rows


def chart_anchor_weight(panel: dict[str, Any]) -> int:
    return max(90, min(110, int(read_float(panel.get("layoutWeight"), 100.0))))


def chart_add_panel_priorities(
    panels: list[dict[str, Any]],
    target_panel_id: str,
    anchor_panel_id: str | None,
) -> list[dict[str, Any]]:
    priorities = []
    for panel in panels:
        if panel["id"] == target_panel_id:
            weight = 120
            reason = "Most recent chart request."
        elif panel["id"] == anchor_panel_id or panel["type"] == "chart":
            weight = max(95, min(110, int(read_float(panel.get("layoutWeight"), 100.0))))
            reason = "Chart panels stay high priority during comparison layouts."
        else:
            weight = max(20, min(50, int(read_float(panel.get("layoutWeight"), 35.0))))
            reason = "Supporting panel compacted below the active chart comparison."
        priorities.append({
            "panelId": panel["id"],
            "panelType": panel["type"],
            "layoutWeight": weight,
            "reason": reason,
        })
    return sorted(priorities, key=lambda item: (-item["layoutWeight"], item["panelId"]))


def ui_intent_from_task(task: UiTask) -> UIIntent:
    return UIIntent(
        isUiIntent=True,
        intentKind="layout",
        targetPanelType=task.targetPanelType,
        targetPanelId=task.targetPanelId,
        action=task.action,
        sizeIntent=task.sizeIntent,
        positionIntent=task.positionIntent,
        confidence=task.confidence,
        reason=task.reason,
        source=task.source,
    )


def multi_ui_target_panel_types(tasks: list[UiTask], panels: list[dict[str, Any]]) -> list[str]:
    panel_type_by_id = {panel["id"]: panel["type"] for panel in panels}
    selected = []
    for task in tasks:
        if task.layoutPreset == "default_workspace":
            selected.extend(DEFAULT_WORKSPACE_PANEL_TYPES)
        elif task.layoutPreset == "visible_workspace":
            selected.extend(panel["type"] for panel in panels)
        selected.extend(task.targetPanelTypes)
        selected.extend(panel_type_by_id[panel_id] for panel_id in task.targetPanelIds if panel_id in panel_type_by_id)
        if task.targetPanelType:
            selected.append(task.targetPanelType)
    return unique_panel_types(selected)


def unique_texts(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def unique_panel_types(panel_types: list[str]) -> list[str]:
    order = ["chart", "newsFeed", "aiSummary", "ontologyGraph", "indicatorCompare", "portfolioHoldings", "stockRecommendations", "orderTicket"]
    selected = {panel_type for panel_type in panel_types if panel_type in order}
    return [panel_type for panel_type in order if panel_type in selected]


def materialize_target_panels(panels: list[dict[str, Any]], panel_types: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    grid = grid_for_panels(panels)
    targets = []
    add_panel_types = []
    for panel_type in panel_types:
        existing = first_panel_of_type(panels, panel_type)
        if existing:
            targets.append(existing)
            continue
        add_panel_types.append(panel_type)
        targets.append({
            "id": proposed_panel_id(panel_type),
            "type": panel_type,
            "title": default_panel_title(panel_type),
            "placement": default_panel_placement(panel_type),
            "layoutPinned": False,
            "layoutWeight": 50,
            "minSpan": default_min_span(panel_type),
            "maxSpan": {"colSpan": grid.cols, "rowSpan": grid.rows},
            "_grid": grid,
        })
    return targets, add_panel_types


def proposed_panel_id(panel_type: str) -> str:
    return {
        "chart": "panel-chart-primary",
        "newsFeed": "panel-news",
        "indicatorCompare": "panel-indicator-compare",
        "orderTicket": "panel-order",
        "portfolioHoldings": "panel-portfolio",
        "aiSummary": "panel-ai-summary",
        "ontologyGraph": "panel-ontology",
    }.get(panel_type, f"panel-{panel_type}")


def arrange_panel_set(
    panels: list[dict[str, Any]],
    target_panels: list[dict[str, Any]],
    position_intent: str | None,
) -> list[dict[str, Any]] | None:
    grid = grid_for_panels([*panels, *target_panels])
    target_ids = {panel["id"] for panel in target_panels}
    pinned = [panel for panel in panels if panel.get("layoutPinned")]
    occupied = occupied_cells([panel["placement"] for panel in pinned], grid)
    placements = [
        {"panelId": panel["id"], "placement": panel["placement"], "layoutWeight": 100}
        for panel in target_panels
        if panel.get("layoutPinned")
    ]
    occupied.update(occupied_cells([item["placement"] for item in placements], grid))

    for panel in [item for item in target_panels if not item.get("layoutPinned")]:
        placement = best_panel_set_placement(occupied, panel, position_intent, grid)
        if not placement:
            return None
        occupied.update(cells_for_placement(placement, grid))
        placements.append({"panelId": panel["id"], "placement": placement, "layoutWeight": 100})

    support = [
        panel
        for panel in panels
        if panel["id"] not in target_ids and not panel.get("layoutPinned")
    ]
    support.sort(key=supporting_panel_sort_key)
    for panel in support:
        placement = compact_supporting_placement(occupied, panel, grid)
        if not placement:
            continue
        occupied.update(cells_for_placement(placement, grid))
        placements.append({
            "panelId": panel["id"],
            "placement": placement,
            "layoutWeight": max(20, min(60, int(panel.get("layoutWeight") or 20))),
        })
    return placements


def best_panel_set_placement(occupied: set[tuple[int, int]], panel: dict[str, Any], position_intent: str | None, grid: WorkspaceGridSpec) -> dict[str, Any] | None:
    for col_span, row_span in preferred_panel_set_spans(panel):
        for col, row in workspace_positions(col_span, row_span, position_intent, grid):
            placement = workspace_placement(col, row, col_span, row_span, grid)
            if not cells_for_placement(placement, grid).intersection(occupied):
                return placement
    return None


def preferred_panel_set_spans(panel: dict[str, Any]) -> list[tuple[int, int]]:
    preferred = {
        "chart": (3, 3),
        "newsFeed": (2, 2),
        "aiSummary": (2, 2),
        "ontologyGraph": (2, 2),
        "indicatorCompare": (2, 2),
        "portfolioHoldings": (1, 2),
        "orderTicket": (1, 2),
    }.get(panel["type"], (1, 1))
    min_span = panel.get("minSpan") if isinstance(panel.get("minSpan"), dict) else default_min_span(panel["type"])
    min_cols = read_int(min_span.get("colSpan"), 1)
    min_rows = read_int(min_span.get("rowSpan"), 1)
    max_span = panel.get("maxSpan") if isinstance(panel.get("maxSpan"), dict) else default_max_span(panel["type"])
    max_cols = read_int(max_span.get("colSpan"), DEFAULT_WORKSPACE_GRID.cols)
    max_rows = read_int(max_span.get("rowSpan"), 5)
    preferred_cols = min(max_cols, max(min_cols, preferred[0]))
    preferred_rows = min(max_rows, max(min_rows, preferred[1]))
    spans = []
    for col_span in range(preferred_cols, min_cols - 1, -1):
        for row_span in range(preferred_rows, min_rows - 1, -1):
            spans.append((col_span, row_span))
    return sorted(set(spans), key=lambda span: (-span[0] * span[1], -span[0]))


def multi_ui_panel_priorities(panels: list[dict[str, Any]], target_panel_ids: list[str]) -> list[dict[str, Any]]:
    target_ids = set(target_panel_ids)
    priorities = []
    for panel in panels:
        is_target = panel["id"] in target_ids
        priorities.append({
            "panelId": panel["id"],
            "panelType": panel["type"],
            "layoutWeight": 100 if is_target else max(20, min(60, int(panel.get("layoutWeight") or 20))),
            "reason": "Target panel for the multi-panel UI request." if is_target else "Supporting panel preserved by the UI agent.",
        })
    return sorted(priorities, key=lambda item: (-item["layoutWeight"], item["panelId"]))


def normalize_layout_panels(layout_context: dict[str, Any]) -> list[dict[str, Any]]:
    panels = layout_context.get("panels") if isinstance(layout_context, dict) else None
    if not isinstance(panels, list):
        return []

    grid = layout_grid_spec(layout_context)
    normalized = []
    for item in panels:
        if not isinstance(item, dict):
            continue
        panel_id = str(item.get("id") or "").strip()
        panel_type = str(item.get("type") or "").strip()
        placement = item.get("placement") if isinstance(item.get("placement"), dict) else {}
        props = item.get("props") if isinstance(item.get("props"), dict) else {}
        symbol = str(item.get("symbol") or props.get("symbol") or "").strip().upper()
        if not panel_id or not panel_type:
            continue
        panel = {
            "id": panel_id,
            "type": panel_type,
            "title": str(item.get("title") or default_panel_title(panel_type)),
            "placement": {
                "group": str(placement.get("group") or "workspace"),
                "zone": str(placement.get("zone") or "main"),
                "col": read_int(placement.get("col"), 1),
                "row": read_int(placement.get("row"), 1),
                "colSpan": read_int(placement.get("colSpan"), 1),
                "rowSpan": read_int(placement.get("rowSpan"), 1),
            },
            "layoutPinned": bool(item.get("layoutPinned")),
            "layoutWeight": read_float(item.get("layoutWeight"), 0.0),
            "minSpan": read_span(item.get("minSpan"), default_min_span(panel_type)),
            "maxSpan": read_span(item.get("maxSpan"), {"colSpan": grid.cols, "rowSpan": grid.rows}),
            "_grid": grid,
        }
        if panel_type == "chart" and symbol:
            panel["symbol"] = symbol
        if props:
            panel["props"] = props
        normalized.append(panel)
    return normalized


def layout_grid_spec(layout_context: dict[str, Any]) -> WorkspaceGridSpec:
    grid = layout_context.get("grid") if isinstance(layout_context, dict) else None
    if not isinstance(grid, dict):
        grid = layout_context.get("workspaceGrid") if isinstance(layout_context, dict) else None
    if not isinstance(grid, dict):
        if str(layout_context.get("version") or "") == "1":
            return WorkspaceGridSpec(cols=4, rows=5)
        return DEFAULT_WORKSPACE_GRID
    cols = max(1, read_int(grid.get("cols"), DEFAULT_WORKSPACE_GRID.cols))
    rows = max(1, read_int(grid.get("rows"), DEFAULT_WORKSPACE_GRID.rows))
    return WorkspaceGridSpec(cols=cols, rows=rows)


def grid_for_panels(panels: list[dict[str, Any]]) -> WorkspaceGridSpec:
    for panel in panels:
        grid = panel.get("_grid")
        if isinstance(grid, WorkspaceGridSpec):
            return grid
    return DEFAULT_WORKSPACE_GRID


def grid_for_panel(panel: dict[str, Any]) -> WorkspaceGridSpec:
    grid = panel.get("_grid")
    return grid if isinstance(grid, WorkspaceGridSpec) else DEFAULT_WORKSPACE_GRID


def resolve_ui_target_panel(panels: list[dict[str, Any]], ui_intent: UIIntent) -> dict[str, Any] | None:
    if ui_intent.targetPanelId:
        panel = next((item for item in panels if item["id"] == ui_intent.targetPanelId), None)
        if panel:
            return panel
    if ui_intent.targetPanelType:
        return first_panel_of_type(panels, ui_intent.targetPanelType)
    return None


def ui_add_command(ui_intent: UIIntent) -> dict[str, Any] | None:
    if ui_intent.action != "open" or not ui_intent.targetPanelType:
        return None
    return layout_command(
        "layout.panel.add",
        {
            "panelType": ui_intent.targetPanelType,
            "placement": default_panel_placement(ui_intent.targetPanelType),
        },
    )


def ui_panel_priorities(panels: list[dict[str, Any]], target_panel_id: str) -> list[dict[str, Any]]:
    priorities = []
    for panel in panels:
        is_target = panel["id"] == target_panel_id
        priorities.append({
            "panelId": panel["id"],
            "panelType": panel["type"],
            "layoutWeight": 100 if is_target else max(20, min(60, int(panel.get("layoutWeight") or 20))),
            "reason": "Target panel for the UI request." if is_target else "Supporting panel preserved by the UI agent.",
        })
    return sorted(priorities, key=lambda item: (-item["layoutWeight"], item["panelId"]))


def arrange_ui_panels(
    panels: list[dict[str, Any]],
    target_panel: dict[str, Any],
    ui_intent: UIIntent,
) -> list[dict[str, Any]] | None:
    grid = grid_for_panels(panels)
    pinned_panels = [
        panel
        for panel in panels
        if panel["id"] != target_panel["id"] and panel.get("layoutPinned")
    ]

    movable = [
        panel
        for panel in panels
        if panel["id"] != target_panel["id"] and not panel.get("layoutPinned")
    ]
    movable.sort(key=supporting_panel_sort_key)

    for target_placement in target_ui_placement_candidates(target_panel, ui_intent):
        if any(overlaps(target_placement, panel["placement"]) for panel in pinned_panels):
            continue

        occupied = occupied_cells([panel["placement"] for panel in pinned_panels] + [target_placement], grid)
        placements = [{
            "panelId": target_panel["id"],
            "placement": target_placement,
            "layoutWeight": 100,
        }]
        packed = True

        for panel in movable:
            placement = compact_supporting_placement(occupied, panel, grid)
            if not placement:
                packed = False
                break
            occupied.update(cells_for_placement(placement, grid))
            placements.append({
                "panelId": panel["id"],
                "placement": placement,
                "layoutWeight": max(20, min(60, int(panel.get("layoutWeight") or 20))),
            })

        if packed:
            return placements

    return None


def target_ui_placement_candidates(panel: dict[str, Any], ui_intent: UIIntent) -> list[dict[str, Any]]:
    grid = grid_for_panel(panel)
    min_span = panel.get("minSpan") if isinstance(panel.get("minSpan"), dict) else default_min_span(panel["type"])
    max_span = panel.get("maxSpan") if isinstance(panel.get("maxSpan"), dict) else default_max_span(panel["type"])
    min_cols = read_int(min_span.get("colSpan"), 1)
    min_rows = read_int(min_span.get("rowSpan"), 1)
    max_cols = read_int(max_span.get("colSpan"), grid.cols)
    max_rows = read_int(max_span.get("rowSpan"), grid.rows)

    current = panel["placement"]
    if ui_intent.sizeIntent == "max":
        desired_cols = min(max_cols, max(min_cols, grid.cols - 1 if grid.cols > 1 else grid.cols))
        desired_rows = min(max_rows, max(min_rows, grid.rows))
    elif ui_intent.sizeIntent == "large" or ui_intent.action in {"focus", "open"}:
        desired_cols = min(max_cols, max(grid.cols if panel["type"] == "chart" else 3, min_cols))
        desired_rows = min(max_rows, max(3, min_rows))
    elif ui_intent.sizeIntent in {"small", "min"}:
        desired_cols = min_cols
        desired_rows = min_rows
    else:
        desired_cols = min(max_cols, max(min_cols, read_int(current.get("colSpan"), min_cols)))
        desired_rows = min(max_rows, max(min_rows, read_int(current.get("rowSpan"), min_rows)))

    spans: list[tuple[int, int]] = []
    for col_span in range(desired_cols, min_cols - 1, -1):
        for row_span in range(desired_rows, min_rows - 1, -1):
            spans.append((col_span, row_span))
    spans = sorted(set(spans), key=lambda span: (-span[0] * span[1], abs(span[0] - desired_cols) + abs(span[1] - desired_rows), -span[0]))

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for col_span, row_span in spans:
        for col, row in workspace_positions(col_span, row_span, ui_intent.positionIntent, grid):
            key = (col, row, col_span, row_span)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(workspace_placement(col, row, col_span, row_span, grid))
    return candidates


def workspace_positions(col_span: int, row_span: int, position_intent: str | None, grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> list[tuple[int, int]]:
    positions = [
        (col, row)
        for row in range(1, grid.rows - row_span + 2)
        for col in range(1, grid.cols - col_span + 2)
    ]
    if position_intent == "bottom":
        return sorted(positions, key=lambda item: (-item[1], item[0]))
    if position_intent == "right":
        return sorted(positions, key=lambda item: (-item[0], item[1]))
    if position_intent == "left":
        return sorted(positions, key=lambda item: (item[0], item[1]))
    if position_intent == "center":
        center_col = (grid.cols + 1) / 2
        center_row = (grid.rows + 1) / 2
        return sorted(positions, key=lambda item: (abs((item[0] + (col_span - 1) / 2) - center_col) + abs((item[1] + (row_span - 1) / 2) - center_row), item[1], item[0]))
    return sorted(positions, key=lambda item: (item[1], item[0]))


def compact_supporting_placement(occupied: set[tuple[int, int]], panel: dict[str, Any], grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> dict[str, Any] | None:
    span = panel.get("minSpan") if isinstance(panel.get("minSpan"), dict) else default_min_span(panel["type"])
    col_span = read_int(span.get("colSpan"), 1)
    row_span = read_int(span.get("rowSpan"), 1)
    return first_available_placement(occupied, col_span, row_span, grid)


def supporting_panel_sort_key(panel: dict[str, Any]) -> tuple[int, int, str]:
    type_rank = {
        "chart": 0,
        "orderTicket": 1,
        "portfolioHoldings": 2,
        "stockRecommendations": 3,
        "ontologyGraph": 4,
        "indicatorCompare": 5,
        "newsFeed": 6,
        "aiSummary": 7,
    }.get(panel["type"], 9)
    return (-int(read_float(panel.get("layoutWeight"), 0.0)), type_rank, panel["id"])


def first_available_placement(occupied: set[tuple[int, int]], col_span: int, row_span: int, grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> dict[str, Any] | None:
    for row in range(1, grid.rows - row_span + 2):
        for col in range(1, grid.cols - col_span + 2):
            placement = workspace_placement(col, row, col_span, row_span, grid)
            cells = cells_for_placement(placement, grid)
            if not cells.intersection(occupied):
                return placement
    return None


def occupied_cells(placements: list[dict[str, Any]], grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> set[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for placement in placements:
        cells.update(cells_for_placement(placement, grid))
    return cells


def cells_for_placement(placement: dict[str, Any], grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> set[tuple[int, int]]:
    if placement.get("group") != "workspace":
        return set()
    col = read_int(placement.get("col"), 1)
    row = read_int(placement.get("row"), 1)
    col_span = read_int(placement.get("colSpan"), 1)
    row_span = read_int(placement.get("rowSpan"), 1)
    return {
        (cell_col, cell_row)
        for cell_col in range(col, col + col_span)
        for cell_row in range(row, row + row_span)
        if 1 <= cell_col <= grid.cols and 1 <= cell_row <= grid.rows
    }


def workspace_placement(col: int, row: int, col_span: int, row_span: int, grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> dict[str, Any]:
    return {
        "group": "workspace",
        "zone": workspace_zone(col, col_span, grid),
        "col": col,
        "row": row,
        "colSpan": col_span,
        "rowSpan": row_span,
    }


def workspace_zone(col: int, col_span: int, grid: WorkspaceGridSpec = DEFAULT_WORKSPACE_GRID) -> str:
    context_start = max(1, grid.cols - 1)
    main_end = max(1, context_start - 1)
    if col >= context_start:
        return "context"
    if col + col_span - 1 <= main_end:
        return "main"
    return "mainContext"


def default_panel_title(panel_type: str) -> str:
    return {
        "chart": "차트",
        "newsFeed": "시장 뉴스",
        "indicatorCompare": "지표 비교",
        "orderTicket": "주문",
        "portfolioHoldings": "내 투자",
        "stockRecommendations": "추천",
        "aiSummary": "AI 요약",
        "ontologyGraph": "온톨로지",
    }.get(panel_type, panel_type)


def default_min_span(panel_type: str) -> dict[str, int]:
    return {"colSpan": 1, "rowSpan": 1}


def default_max_span(panel_type: str) -> dict[str, int]:
    return {"colSpan": DEFAULT_WORKSPACE_GRID.cols, "rowSpan": DEFAULT_WORKSPACE_GRID.rows}


def default_panel_placement(panel_type: str) -> dict[str, Any]:
    if panel_type == "chart":
        return workspace_placement(1, 4, DEFAULT_WORKSPACE_GRID.cols, 2)
    if panel_type == "ontologyGraph":
        return workspace_placement(5, 1, 4, 2)
    if panel_type == "orderTicket":
        return workspace_placement(7, 1, 2, 1)
    if panel_type == "portfolioHoldings":
        return workspace_placement(1, 1, 4, 1)
    if panel_type == "stockRecommendations":
        return workspace_placement(5, 1, 4, 1)
    return workspace_placement(1, 1, 4, 1)


def read_span(value: Any, fallback: dict[str, int]) -> dict[str, int]:
    if not isinstance(value, dict):
        return dict(fallback)
    return {
        "colSpan": read_int(value.get("colSpan"), fallback["colSpan"]),
        "rowSpan": read_int(value.get("rowSpan"), fallback["rowSpan"]),
    }


def primary_panel_type_for_route(route, context: AgentContext) -> str:
    intent_type = str(getattr(route, "intentType", "") or "").lower()
    selected_roles = [str(role).lower() for role in getattr(route, "selectedRoles", []) or []]
    intent_text = context.intent.lower()
    if "market-move" in intent_type or context.marketEvents:
        return "chart"
    if "news" in intent_type or "news" in selected_roles or any(token in intent_text for token in ("news", "headline", "뉴스", "기사", "보도")):
        return "newsFeed"
    if "ontology" in intent_type or "ontology" in selected_roles or any(token in intent_text for token in ("relationship", "supply", "관계", "온톨로지", "공급망", "경쟁사", "섹터")):
        return "ontologyGraph"
    if any(token in intent_text for token in ("portfolio", "holdings", "balance", "보유종목", "잔고", "내 투자", "계좌")):
        return "portfolioHoldings"
    if any(token in intent_text for token in ("recommendation", "recommend", "buy idea", "종목 추천", "매수 추천", "추천 종목", "장중 추천")):
        return "stockRecommendations"
    if "macro" in intent_type or "macro" in selected_roles or any(token in intent_text for token in ("macro", "rate", "inflation", "거시", "금리")):
        return "indicatorCompare"
    return "chart"


def panel_priorities_for_route(
    panels: list[dict[str, Any]],
    primary_type: str,
    route,
    context: AgentContext,
) -> list[dict[str, Any]]:
    role_types = {
        "chart": "chart",
        "news": "newsFeed",
        "macro": "indicatorCompare",
        "ontology": "ontologyGraph",
    }
    selected_panel_types = [
        role_types[role]
        for role in getattr(route, "selectedRoles", []) or []
        if role in role_types
    ]
    weights = {
        "chart": 60,
        "newsFeed": 52,
        "indicatorCompare": 48,
        "ontologyGraph": 48,
        "portfolioHoldings": 44,
        "stockRecommendations": 46,
        "aiSummary": 50,
        "orderTicket": 5,
    }
    weights[primary_type] = 100
    for index, panel_type in enumerate(selected_panel_types):
        weights[panel_type] = max(weights.get(panel_type, 0), 86 - index * 8)
    if context.marketEvents:
        weights["aiSummary"] = max(weights.get("aiSummary", 0), 82)
        weights["newsFeed"] = max(weights.get("newsFeed", 0), 76)

    priorities = []
    for panel in panels:
        panel_type = panel["type"]
        weight = weights.get(panel_type, 25)
        priorities.append({
            "panelId": panel["id"],
            "panelType": panel_type,
            "layoutWeight": weight,
            "reason": priority_reason(panel_type, primary_type),
        })
    return sorted(priorities, key=lambda item: (-item["layoutWeight"], item["panelId"]))


def priority_reason(panel_type: str, primary_type: str) -> str:
    if panel_type == primary_type:
        return "Primary panel for the current user intent."
    if panel_type == "orderTicket":
        return "Order entry stays low priority during analysis-only layout changes."
    return "Supporting panel for the current user intent."


def priority_commands(priorities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        layout_command(
            "layout.panel.priority.set",
            {"panelId": item["panelId"], "layoutWeight": item["layoutWeight"]},
            {"panelId": item["panelId"]},
        )
        for item in priorities
    ]


def first_panel_of_type(panels: list[dict[str, Any]], panel_type: str) -> dict[str, Any] | None:
    return next((panel for panel in panels if panel["type"] == panel_type), None)


def primary_panel_move_command(panels: list[dict[str, Any]], panel: dict[str, Any]) -> dict[str, Any] | None:
    if panel.get("layoutPinned"):
        return None
    placement = panel["placement"]
    if placement.get("group") != "workspace":
        return None
    target = {
        **placement,
        "group": "workspace",
        "zone": "main",
        "col": 1,
        "row": 1,
    }
    if placement.get("col") == target["col"] and placement.get("row") == target["row"]:
        return None
    if any(overlaps(target, other["placement"]) and other.get("layoutPinned") for other in panels if other["id"] != panel["id"]):
        return None
    return layout_command("layout.panel.move", {"panelId": panel["id"], "placement": target}, {"panelId": panel["id"]})


def overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("group") != right.get("group"):
        return False
    left_col_end = int(left.get("col", 1)) + int(left.get("colSpan", 1)) - 1
    right_col_end = int(right.get("col", 1)) + int(right.get("colSpan", 1)) - 1
    left_row_end = int(left.get("row", 1)) + int(left.get("rowSpan", 1)) - 1
    right_row_end = int(right.get("row", 1)) + int(right.get("rowSpan", 1)) - 1
    return not (
        left_col_end < int(right.get("col", 1)) or
        right_col_end < int(left.get("col", 1)) or
        left_row_end < int(right.get("row", 1)) or
        right_row_end < int(left.get("row", 1))
    )


def layout_command(command_type: str, payload: dict[str, Any], target: dict[str, Any] | None = None) -> dict[str, Any]:
    created_at = utc_now_iso()
    command = {
        "id": stable_id("layout-command", {"type": command_type, "payload": payload, "target": target, "createdAt": created_at}),
        "type": command_type,
        "actor": "llm",
        "payload": payload,
        "createdAt": created_at,
    }
    if target:
        command["target"] = target
    return command


def read_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def read_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def severity_rank(value: str) -> int:
    order = {"none": 0, "info": 1, "watch": 2, "alert": 3, "critical": 4}
    return order.get(value, 1)


def analyze_news_evidence(context: AgentContext, evidence: list[EvidenceItem]) -> dict[str, Any]:
    available = [item for item in evidence if item.status == "available"]
    if not available:
        summary = f"{context.symbol} 관련 저장 뉴스 근거를 확인하지 못했습니다."
        detail = evidence[0].summary if evidence else "뉴스 provider에서 반환된 근거가 없습니다."
        return {
            "summary": summary,
            "rationale": f"뉴스 근거 상태: {detail}",
            "confidence": 0.35,
            "tags": ["news", "no-data"],
        }

    directions = Counter(news_raw_value(item, "impactDirection", "unknown") for item in available)
    events = Counter(news_raw_value(item, "eventType", "other") for item in available)
    dominant_direction = dominant_label(directions, fallback="unknown")
    dominant_event = dominant_label(events, fallback="other")
    top_titles = [display_news_title(item) for item in available[:3]]
    summary = (
        f"{context.symbol} 뉴스 {len(available)}건을 확인했습니다. "
        f"주요 이벤트는 {event_type_label(dominant_event)}이고, "
        f"주가 영향 방향은 {impact_direction_label(dominant_direction)}로 분류했습니다."
    )
    rationale = "핵심 뉴스: " + "; ".join(top_titles)
    return {
        "summary": summary,
        "rationale": rationale,
        "confidence": 0.68,
        "tags": ["news", "analysis", f"impact:{dominant_direction}", f"event:{dominant_event}"],
    }


def analyze_ontology_evidence(context: AgentContext, evidence: list[EvidenceItem]) -> dict[str, Any]:
    available = [item for item in evidence if item.status == "available"]
    no_data = [item for item in evidence if item.status == "no-data"]
    relation_types = Counter(news_raw_value(item, "relationType", "ontology") for item in evidence)
    themes = unique_values(item.raw.get("themeName") for item in available if isinstance(item.raw, dict))
    controls = [
        item
        for item in available
        if news_raw_value(item, "relationType", "") in {"control", "theme-control"}
    ]
    graphdb_unavailable = any(news_raw_value(item, "relationType", "") == "graphdb-unavailable" for item in no_data)
    no_direct = any(news_raw_value(item, "relationType", "") == "no-direct-control" for item in no_data)

    if graphdb_unavailable:
        detail = (no_data[0].summary if no_data else "기업 관계 데이터 조회에 실패했습니다.").replace("GraphDB", "기업 관계 데이터")
        return {
            "summary": f"{context.symbol} 기업 관계 분석을 완료하지 못했습니다.",
            "rationale": f"기업 관계 데이터 연결 실패: {detail}",
            "confidence": 0.25,
            "tags": ["ontology", "graphdb-unavailable"],
        }

    if not available:
        detail = next((item.summary for item in no_data if item.summary), f"{context.symbol} 관계 근거가 없습니다.").replace("GraphDB", "기업 관계 데이터")
        return {
            "summary": f"{context.symbol} 관련 온톨로지 관계 근거를 확인하지 못했습니다.",
            "rationale": detail,
            "confidence": 0.34,
            "tags": ["ontology", "no-data", *relation_type_tags(relation_types)],
        }

    theme_text = ", ".join(themes[:3]) if themes else "확인된 테마"
    if controls:
        controlled_names = unique_values(
            item.raw.get("controlledName")
            for item in controls
            if isinstance(item.raw, dict) and item.raw.get("controlledName")
        )
        control_text = ", ".join(controlled_names[:3]) if controlled_names else "확인된 기업"
        summary = f"관계 데이터 기준으로 {context.symbol}는 {theme_text} 관계와 {control_text} 직접 지배/자회사 관계 근거가 있습니다."
    elif no_direct:
        summary = f"관계 데이터 기준으로 {context.symbol}는 {theme_text} 테마에 속합니다. 직접 지배/자회사 관계 근거는 확인되지 않았습니다."
    else:
        summary = f"관계 데이터 기준으로 {context.symbol}는 {theme_text} 관계 근거가 있습니다."

    evidence_lines = [item.summary for item in available[:4]]
    if no_direct:
        evidence_lines.extend(item.summary for item in no_data if news_raw_value(item, "relationType", "") == "no-direct-control")
    return {
        "summary": summary,
        "rationale": " / ".join(evidence_lines),
        "confidence": 0.66,
        "tags": ["ontology", "analysis", *relation_type_tags(relation_types)],
    }


def role_analysis_with_openai(
    *,
    role: str,
    context: AgentContext,
    evidence: list[EvidenceItem],
    fallback: dict[str, Any],
    schema_name: str,
) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not runtime_role_openai_allowed(role):
        return None
    runtime_context = getattr(context, "runtimeContext", None)
    if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
        if not runtime_context.acquire_llm(f"role:{role}"):
            return None
    try:
        if runtime_context is None:
            add_context_timing_count(context, "llmCalls", 1)
        payload = {
            "model": os.getenv("AGENT_ROLE_ANALYSIS_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are a stock-analysis role agent. Analyze only the supplied evidence. "
                        "Do not invent news, relationships, prices, sources, recommendations, or citations. "
                        "Write concise Korean. Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "role": role,
                            "symbol": context.symbol,
                            "intent": context.intent,
                            "evidence": compact_role_evidence(evidence),
                            "deterministicFallback": fallback,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "summary": {"type": "string"},
                            "rationale": {"type": "string"},
                            "confidence": {"type": "number"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["summary", "rationale", "confidence", "tags"],
                    },
                }
            },
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_ROLE_ANALYSIS_TIMEOUT_SECONDS", "10"))) as response:
            data = json.loads(response.read().decode("utf-8"))
        parsed = parse_openai_text_json(data)
        summary = parsed.get("summary")
        rationale = parsed.get("rationale")
        tags = parsed.get("tags")
        confidence = parsed.get("confidence")
        if not isinstance(summary, str) or not isinstance(rationale, str) or not isinstance(tags, list):
            return None
        return {
            "summary": summary,
            "rationale": rationale,
            "confidence": float(confidence) if isinstance(confidence, (int, float)) else fallback["confidence"],
            "tags": [str(item) for item in tags],
        }
    except Exception:
        return None


def compact_role_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    compacted = []
    for item in items[:12]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        title = display_news_title(item) if item.provider == "news" else item.title
        summary = display_news_summary(item) if item.provider == "news" else item.summary
        compacted.append({
            "provider": item.provider,
            "status": item.status,
            "title": title,
            "summary": summary,
            "url": item.url,
            "raw": {
                key: raw.get(key)
                for key in [
                    "impactDirection",
                    "eventType",
                    "subjectRelevance",
                    "relevanceScore",
                    "relevanceScoreV2",
                    "relevanceReason",
                    "directSignals",
                    "importanceScore",
                    "publishedAt",
                    "source",
                    "symbol",
                    "symbols",
                    "originalTitle",
                    "originalSummary",
                    "localizedTitle",
                    "localizedSummary",
                    "relationType",
                    "themeName",
                    "controlledName",
                    "confidence",
                    "accession",
                    "sourceUrl",
                ]
                if key in raw
            },
        })
    return compacted


def build_news_panel_props(symbol: str, evidence: list[EvidenceItem], daily_summaries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    news_items = [item for item in evidence if item.provider == "news" and item.status == "available"]
    no_data = [item for item in evidence if item.provider == "news" and item.status == "no-data"]
    if not news_items and not no_data and not daily_summaries:
        return None
    latest = sorted(news_items, key=lambda item: parse_panel_time(item), reverse=True)
    major = sorted(
        news_items,
        key=lambda item: (
            panel_raw_number(item, "importanceScore"),
            panel_raw_number(item, "relevanceScore"),
            parse_panel_time(item),
        ),
        reverse=True,
    )
    return {
        "symbol": symbol,
        "updatedAt": utc_now_iso(),
        "displayMode": "dailySummary" if daily_summaries else "articleList",
        "status": "available" if news_items or daily_summaries else "empty",
        "emptyMessage": no_data[0].summary if no_data else f"{symbol} 관련 저장 뉴스가 없습니다.",
        "dailySummaries": [daily_summary_panel_item(item) for item in (daily_summaries or [])[:5]],
        "latestNews": [news_panel_item(item, symbol) for item in latest[:12]],
        "majorNews": [news_panel_item(item, symbol) for item in major[:8]],
    }


def should_add_news_panel_without_layout(props: dict[str, Any] | None) -> bool:
    if not props:
        return False
    return bool(props.get("latestNews") or props.get("majorNews") or props.get("dailySummaries"))


def news_panel_add_command(props: dict[str, Any]) -> dict[str, Any]:
    return layout_command(
        "layout.panel.add",
        {
            "panelType": "newsFeed",
            "props": props,
        },
    )


def news_panel_props_command(panels: list[dict[str, Any]], props: dict[str, Any]) -> dict[str, Any]:
    panel = first_panel_of_type(panels, "newsFeed")
    if not panel:
        return news_panel_add_command(props)
    return layout_command(
        "layout.panel.props.update",
        {
            "panelId": panel["id"],
            "props": props,
        },
        {"panelId": panel["id"]},
    )


def news_panel_item(item: EvidenceItem, symbol: str) -> dict[str, Any]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    symbols = raw.get("symbols") if isinstance(raw.get("symbols"), list) else [raw.get("symbol") or symbol]
    return {
        "title": display_news_title(item),
        "summary": display_news_summary(item),
        "localizedTitle": news_raw_optional(item, "localizedTitle"),
        "localizedSummary": news_raw_optional(item, "localizedSummary"),
        "originalTitle": news_raw_optional(item, "originalTitle") or item.title,
        "originalSummary": news_raw_optional(item, "originalSummary") or item.summary,
        "url": item.url,
        "source": raw.get("source") or item.provider,
        "publishedAt": raw.get("publishedAt") or item.observedAt,
        "symbol": raw.get("symbol") or symbol,
        "symbols": [str(value) for value in symbols if value],
        "eventType": raw.get("eventType") or "other",
        "impactDirection": raw.get("impactDirection") or "unknown",
        "subjectRelevance": raw.get("subjectRelevance") or "mention",
        "relevanceReason": raw.get("relevanceReason"),
        "directSignals": raw.get("directSignals") or [],
        "relevanceScore": panel_raw_number(item, "relevanceScore"),
        "relevanceScoreV2": panel_raw_number(item, "relevanceScoreV2"),
        "importanceScore": panel_raw_number(item, "importanceScore"),
    }


def daily_summary_panel_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": str(item.get("date") or ""),
        "symbol": str(item.get("symbol") or "").upper(),
        "summary": str(item.get("summary") or ""),
        "keyPoints": [str(value) for value in item.get("keyPoints") or [] if str(value).strip()],
        "positivePoints": [str(value) for value in item.get("positivePoints") or [] if str(value).strip()],
        "concerns": [str(value) for value in item.get("concerns") or [] if str(value).strip()],
        "impactDirection": item.get("impactDirection") or "neutral",
        "sentiment": item.get("sentiment") or "neutral",
        "articleIds": [str(value) for value in item.get("articleIds") or [] if str(value).strip()],
        "articleCount": int(item.get("articleCount") or 0),
        "mentionCount": int(item.get("mentionCount") or 0),
        "status": item.get("status") or "rolling",
        "generatedAt": item.get("generatedAt"),
        "sources": daily_summary_sources(item.get("sources")),
        "priceChange": daily_summary_price_change(item.get("priceChange")),
    }


def daily_summary_price_change(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        previous_close = float(value.get("previousClose"))
        close = float(value.get("close"))
        change = float(value.get("change"))
        change_percent = float(value.get("changePercent", 0))
    except (TypeError, ValueError):
        return None
    date = str(value.get("date") or "").strip()
    if not date:
        return None
    return {
        "date": date[:10],
        "previousClose": previous_close,
        "close": close,
        "change": change,
        "changePercent": change_percent,
    }


def daily_summary_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sources = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        key = str(item.get("articleId") or url)
        if key in seen:
            continue
        seen.add(key)
        source = {
            "title": title,
            "url": url,
        }
        for key in ("articleId", "name", "publishedAt"):
            text = str(item.get(key) or "").strip()
            if text:
                source[key] = text
        sources.append(source)
        if len(sources) >= 3:
            break
    return sources


def panel_raw_number(item: EvidenceItem, key: str) -> float:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def display_news_title(item: EvidenceItem) -> str:
    return news_raw_optional(item, "localizedTitle") or item.title


def display_news_summary(item: EvidenceItem) -> str:
    return news_raw_optional(item, "localizedSummary") or item.summary


def news_raw_optional(item: EvidenceItem, key: str) -> str | None:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    text = str(value).strip() if value is not None else ""
    return text or None


def parse_panel_time(item: EvidenceItem) -> float:
    raw = item.raw if isinstance(item.raw, dict) else {}
    text = str(raw.get("publishedAt") or raw.get("receivedAt") or item.observedAt or "")
    try:
        return datetime_from_iso(text)
    except Exception:
        return 0.0


def datetime_from_iso(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def detect_cross_agent_conflicts(findings: list[AgentFinding]) -> list[str]:
    chart_direction = chart_price_direction(findings)
    news_direction = news_impact_direction(findings)
    conflicts = []
    if chart_direction == "down" and news_direction == "positive":
        conflicts.append("뉴스 방향은 긍정적이지만 차트 가격 반응은 하락으로 나타나 불일치가 있습니다.")
    elif chart_direction == "up" and news_direction == "negative":
        conflicts.append("뉴스 방향은 부정적이지만 차트 가격 반응은 상승으로 나타나 불일치가 있습니다.")
    return conflicts


def chart_price_direction(findings: list[AgentFinding]) -> str | None:
    chart_finding = next((item for item in findings if item.role == "chart-analysis"), None)
    if not chart_finding:
        return None
    for evidence in chart_finding.evidence:
        raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        visible_summary = raw.get("visibleSummary")
        change = visible_summary.get("change") if isinstance(visible_summary, dict) else None
        direction = parse_change_direction(change)
        if direction:
            return direction
    return None


def news_impact_direction(findings: list[AgentFinding]) -> str | None:
    news_finding = next((item for item in findings if item.role == "news-analysis"), None)
    if not news_finding:
        return None
    directions = Counter(
        news_raw_value(evidence, "impactDirection", "unknown")
        for evidence in news_finding.evidence
        if evidence.status == "available"
    )
    direction = dominant_label(directions, fallback="unknown")
    return direction if direction in {"positive", "negative"} else None


def parse_change_direction(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed > 0:
        return "up"
    if parsed < 0:
        return "down"
    return None


def news_raw_value(item: EvidenceItem, key: str, fallback: str) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return str(value) if value else fallback


def dominant_label(counter: Counter, *, fallback: str) -> str:
    if not counter:
        return fallback
    for label, _count in counter.most_common():
        if label != "unknown":
            return label
    return fallback


def unique_values(values) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def relation_type_tags(counter: Counter) -> list[str]:
    return [f"relation:{label}" for label in sorted(counter) if label]


def impact_direction_label(value: str) -> str:
    labels = {
        "positive": "긍정",
        "negative": "부정",
        "mixed": "혼재",
        "unknown": "판단 보류",
    }
    return labels.get(value, value)


def event_type_label(value: str) -> str:
    labels = {
        "earnings": "실적",
        "guidance": "가이던스",
        "product": "제품/기술",
        "regulation": "규제",
        "analyst": "애널리스트 의견",
        "macro": "거시 지표",
        "mna": "인수합병",
        "legal": "법무 이슈",
        "partnership": "제휴",
        "other": "기타",
    }
    return labels.get(value, value)
