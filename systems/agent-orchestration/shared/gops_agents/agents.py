from __future__ import annotations

import json
import os
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .contracts import AgentFinding, EvidenceItem, LayoutProposal, MarketEvent, NotificationDecision, stable_id, utc_now_iso
from .news_localization import NewsLocalizationService
from .providers import ClickHouseNewsProvider, EmptyMacroProvider, GraphDBOntologyProvider, ProviderRequest
from .router import parse_openai_text_json
from .ui_intent import UIIntent


@dataclass
class AgentContext:
    symbol: str
    intent: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    chartContext: dict[str, Any] = field(default_factory=dict)
    layoutContext: dict[str, Any] = field(default_factory=dict)
    marketEvents: list[MarketEvent] = field(default_factory=list)
    providerEvidence: list[EvidenceItem] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    newsSymbols: list[str] = field(default_factory=list)
    newsTopic: str | None = None
    newsDailySummaries: list[dict[str, Any]] = field(default_factory=list)
    intentType: str | None = None
    selectedRoles: list[str] = field(default_factory=list)


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
        try:
            request = ProviderRequest(context.symbol, context.intent, symbols=tuple(context.newsSymbols))
            evidence = self.provider.fetch(request)
            if hasattr(self.provider, "fetch_daily_summaries"):
                context.newsDailySummaries = list(self.provider.fetch_daily_summaries(request))
        finally:
            add_context_timing_ms(context, "newsFetchMs", (time.perf_counter() - started_at) * 1000)
        evidence = self.localizer.localize(
            symbol=context.symbol,
            intent=context.intent,
            evidence=evidence,
            allow_runtime_openai=not news_only,
        )
        record_news_relevance_counts(context, evidence)
        analysis = analyze_news_evidence(context, evidence)
        openai_analysis = None
        if not news_only:
            openai_analysis = role_analysis_with_openai(
                role="news",
                context=context,
                evidence=evidence,
                fallback=analysis,
                schema_name="news_agent_analysis",
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


def add_context_timing_ms(context: AgentContext, key: str, elapsed_ms: float) -> None:
    current = context.timing.get(key)
    context.timing[key] = (float(current) if isinstance(current, (int, float)) else 0.0) + elapsed_ms


def add_context_timing_count(context: AgentContext, key: str, count: int = 1) -> None:
    current = context.timing.get(key)
    context.timing[key] = (int(current) if isinstance(current, int) else 0) + count


def is_news_only_context(context: AgentContext) -> bool:
    roles = [str(role) for role in context.selectedRoles]
    return context.intentType == "news" and roles == ["news"]


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
        super().__init__(provider or GraphDBOntologyProvider())

    def analyze(self, context: AgentContext) -> AgentFinding:
        evidence = self.provider.fetch(ProviderRequest(context.symbol, context.intent))
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
            has_layout_context = isinstance(context.layoutContext, dict) and "panels" in context.layoutContext
            return LayoutProposal(
                title="Agent analysis workspace",
                rationale="No layout context was supplied, so the layout agent only proposed display panels with available evidence.",
                commands=[news_panel_add_command(news_panel_props)] if should_add_news_panel_without_layout(news_panel_props, include_empty=has_layout_context) else [],
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
    def propose(self, context: AgentContext, ui_intent: UIIntent) -> LayoutProposal:
        panels = normalize_layout_panels(context.layoutContext)
        if not ui_intent.isUiIntent:
            return LayoutProposal(
                title="UI layout request",
                rationale="The request was not classified as a UI layout action.",
                commands=[],
                autoApply=False,
                panelPriorities=[],
            )

        target_panel = resolve_ui_target_panel(panels, ui_intent)
        if not target_panel:
            add_command = ui_add_command(ui_intent)
            return LayoutProposal(
                title="UI layout request",
                rationale=(
                    f"Prepared to open a {ui_intent.targetPanelType} panel."
                    if add_command
                    else "The UI agent could not identify a visible target panel for this request."
                ),
                commands=[add_command] if add_command else [],
                autoApply=bool(add_command),
                panelPriorities=[],
            )

        if target_panel.get("layoutPinned"):
            return LayoutProposal(
                title="UI layout request",
                rationale=f"{target_panel.get('title') or target_panel['id']} is pinned, so the UI agent did not move it.",
                commands=[],
                autoApply=False,
                panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
            )

        if ui_intent.action == "close":
            return LayoutProposal(
                title="UI layout request",
                rationale="Closing or removing panels is not auto-applied by the UI agent.",
                commands=[],
                autoApply=False,
                panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
            )

        placements = arrange_ui_panels(panels, target_panel, ui_intent)
        if not placements:
            return LayoutProposal(
                title="UI layout request",
                rationale="The UI agent could not find a valid panel arrangement for the requested layout.",
                commands=[],
                autoApply=False,
                panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
            )

        return LayoutProposal(
            title="UI layout request",
            rationale=f"UIAgent arranged {target_panel.get('title') or target_panel['type']} for the requested UI action.",
            commands=[
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
            ],
            autoApply=True,
            panelPriorities=ui_panel_priorities(panels, target_panel["id"]),
        )


def normalize_layout_panels(layout_context: dict[str, Any]) -> list[dict[str, Any]]:
    panels = layout_context.get("panels") if isinstance(layout_context, dict) else None
    if not isinstance(panels, list):
        return []

    normalized = []
    for item in panels:
        if not isinstance(item, dict):
            continue
        panel_id = str(item.get("id") or "").strip()
        panel_type = str(item.get("type") or "").strip()
        placement = item.get("placement") if isinstance(item.get("placement"), dict) else {}
        if not panel_id or not panel_type:
            continue
        normalized.append({
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
            "maxSpan": read_span(item.get("maxSpan"), default_max_span(panel_type)),
        })
    return normalized


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

        occupied = occupied_cells([panel["placement"] for panel in pinned_panels] + [target_placement])
        placements = [{
            "panelId": target_panel["id"],
            "placement": target_placement,
            "layoutWeight": 100,
        }]
        packed = True

        for panel in movable:
            placement = compact_supporting_placement(occupied, panel)
            if not placement:
                packed = False
                break
            occupied.update(cells_for_placement(placement))
            placements.append({
                "panelId": panel["id"],
                "placement": placement,
                "layoutWeight": max(20, min(60, int(panel.get("layoutWeight") or 20))),
            })

        if packed:
            return placements

    return None


def target_ui_placement_candidates(panel: dict[str, Any], ui_intent: UIIntent) -> list[dict[str, Any]]:
    min_span = panel.get("minSpan") if isinstance(panel.get("minSpan"), dict) else default_min_span(panel["type"])
    max_span = panel.get("maxSpan") if isinstance(panel.get("maxSpan"), dict) else default_max_span(panel["type"])
    min_cols = read_int(min_span.get("colSpan"), 1)
    min_rows = read_int(min_span.get("rowSpan"), 1)
    max_cols = read_int(max_span.get("colSpan"), 4)
    max_rows = read_int(max_span.get("rowSpan"), 5)

    current = panel["placement"]
    if ui_intent.sizeIntent == "max":
        desired_cols = min(max_cols, max(min_cols, 3))
        desired_rows = min(max_rows, max(min_rows, 5))
    elif ui_intent.sizeIntent == "large" or ui_intent.action in {"focus", "open"}:
        desired_cols = min(max_cols, max(3, min_cols))
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
        for col, row in workspace_positions(col_span, row_span, ui_intent.positionIntent):
            key = (col, row, col_span, row_span)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(workspace_placement(col, row, col_span, row_span))
    return candidates


def workspace_positions(col_span: int, row_span: int, position_intent: str | None) -> list[tuple[int, int]]:
    positions = [
        (col, row)
        for row in range(1, 5 - row_span + 2)
        for col in range(1, 4 - col_span + 2)
    ]
    if position_intent == "bottom":
        return sorted(positions, key=lambda item: (-item[1], item[0]))
    if position_intent == "right":
        return sorted(positions, key=lambda item: (-item[0], item[1]))
    if position_intent == "left":
        return sorted(positions, key=lambda item: (item[0], item[1]))
    if position_intent == "center":
        return sorted(positions, key=lambda item: (abs((item[0] + (col_span - 1) / 2) - 2.5) + abs((item[1] + (row_span - 1) / 2) - 3), item[1], item[0]))
    return sorted(positions, key=lambda item: (item[1], item[0]))


def compact_supporting_placement(occupied: set[tuple[int, int]], panel: dict[str, Any]) -> dict[str, Any] | None:
    span = panel.get("minSpan") if isinstance(panel.get("minSpan"), dict) else default_min_span(panel["type"])
    col_span = read_int(span.get("colSpan"), 1)
    row_span = read_int(span.get("rowSpan"), 1)
    return first_available_placement(occupied, col_span, row_span)


def supporting_panel_sort_key(panel: dict[str, Any]) -> tuple[int, int, str]:
    type_rank = {
        "chart": 0,
        "orderTicket": 1,
        "portfolioHoldings": 2,
        "ontologyGraph": 3,
        "indicatorCompare": 4,
        "newsFeed": 5,
        "aiSummary": 6,
    }.get(panel["type"], 9)
    return (-int(read_float(panel.get("layoutWeight"), 0.0)), type_rank, panel["id"])


def first_available_placement(occupied: set[tuple[int, int]], col_span: int, row_span: int) -> dict[str, Any] | None:
    for row in range(1, 6 - row_span + 1):
        for col in range(1, 5 - col_span + 1):
            placement = workspace_placement(col, row, col_span, row_span)
            cells = cells_for_placement(placement)
            if not cells.intersection(occupied):
                return placement
    return None


def occupied_cells(placements: list[dict[str, Any]]) -> set[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for placement in placements:
        cells.update(cells_for_placement(placement))
    return cells


def cells_for_placement(placement: dict[str, Any]) -> set[tuple[int, int]]:
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
        if 1 <= cell_col <= 4 and 1 <= cell_row <= 5
    }


def workspace_placement(col: int, row: int, col_span: int, row_span: int) -> dict[str, Any]:
    return {
        "group": "workspace",
        "zone": workspace_zone(col, col_span),
        "col": col,
        "row": row,
        "colSpan": col_span,
        "rowSpan": row_span,
    }


def workspace_zone(col: int, col_span: int) -> str:
    if col == 4 and col_span == 1:
        return "context"
    if col + col_span - 1 <= 3:
        return "main"
    return "mainContext"


def default_panel_title(panel_type: str) -> str:
    return {
        "chart": "차트",
        "newsFeed": "시장 뉴스",
        "indicatorCompare": "지표 비교",
        "orderTicket": "주문",
        "portfolioHoldings": "내 투자",
        "aiSummary": "AI 요약",
        "ontologyGraph": "온톨로지",
    }.get(panel_type, panel_type)


def default_min_span(panel_type: str) -> dict[str, int]:
    return {"colSpan": 1, "rowSpan": 2 if panel_type in {"orderTicket", "portfolioHoldings"} else 1}


def default_max_span(panel_type: str) -> dict[str, int]:
    return {"colSpan": 4, "rowSpan": 5}


def default_panel_placement(panel_type: str) -> dict[str, Any]:
    if panel_type == "chart":
        return workspace_placement(1, 1, 3, 3)
    if panel_type == "ontologyGraph":
        return workspace_placement(4, 1, 1, 2)
    if panel_type == "orderTicket":
        return workspace_placement(4, 4, 1, 2)
    if panel_type == "portfolioHoldings":
        return workspace_placement(1, 4, 1, 2)
    return workspace_placement(4, 1, 1, 1)


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
    direct_available = [item for item in available if is_direct_news_item(item)]
    if not available:
        summary = f"{context.symbol} 관련 저장 뉴스 근거를 확인하지 못했습니다."
        detail = evidence[0].summary if evidence else "뉴스 provider에서 반환된 근거가 없습니다."
        return {
            "summary": summary,
            "rationale": f"데이터 한계: {detail}",
            "confidence": 0.35,
            "tags": ["news", "no-data"],
        }
    if not direct_available:
        return {
            "summary": f"{context.symbol} 관련 저장 뉴스 근거를 확인하지 못했습니다.",
            "rationale": f"언급 기사 {len(available)}건은 있었지만 primary/secondary 직접 관련 기사로 분류되지 않았습니다.",
            "confidence": 0.42,
            "tags": ["news", "no-direct-news"],
        }

    directions = Counter(news_raw_value(item, "impactDirection", "unknown") for item in direct_available)
    events = Counter(news_raw_value(item, "eventType", "other") for item in direct_available)
    dominant_direction = dominant_label(directions, fallback="unknown")
    dominant_event = dominant_label(events, fallback="other")
    top_titles = [display_news_title(item) for item in direct_available[:3]]
    summary = (
        f"{context.symbol} 뉴스 {len(direct_available)}건을 확인했습니다. "
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
        detail = no_data[0].summary if no_data else "GraphDB 온톨로지 조회에 실패했습니다."
        return {
            "summary": f"{context.symbol} 기업 관계 분석을 완료하지 못했습니다.",
            "rationale": f"GraphDB 연결 실패: {detail}",
            "confidence": 0.25,
            "tags": ["ontology", "graphdb-unavailable"],
        }

    if not available:
        detail = next((item.summary for item in no_data if item.summary), f"{context.symbol} 관계 근거가 없습니다.")
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
        summary = f"GraphDB 기준으로 {context.symbol}는 {theme_text} 관계와 {control_text} 직접 지배/자회사 관계 근거가 있습니다."
    elif no_direct:
        summary = f"GraphDB 기준으로 {context.symbol}는 {theme_text} 테마에 속합니다. 직접 지배/자회사 관계 근거는 확인되지 않았습니다."
    else:
        summary = f"GraphDB 기준으로 {context.symbol}는 {theme_text} 관계 근거가 있습니다."

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
    if not api_key or os.getenv("AGENT_ROLE_ANALYSIS_PROVIDER") == "deterministic":
        return None
    try:
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
    direct_news_items = [item for item in news_items if is_direct_news_item(item)]
    mention_items = [item for item in news_items if is_mention_news_item(item)]
    no_data = [item for item in evidence if item.provider == "news" and item.status == "no-data"]
    if not direct_news_items and not mention_items and not no_data and not daily_summaries:
        return None
    latest = sorted(direct_news_items, key=lambda item: parse_panel_time(item), reverse=True)
    major = sorted(
        direct_news_items,
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
        "status": "available" if direct_news_items or daily_summaries else "empty",
        "emptyMessage": no_data[0].summary if no_data else f"{symbol} 관련 저장 뉴스가 없습니다.",
        "dailySummaries": [daily_summary_panel_item(item) for item in (daily_summaries or [])[:5]],
        "latestNews": [news_panel_item(item, symbol) for item in latest[:12]],
        "majorNews": [news_panel_item(item, symbol) for item in major[:8]],
        "mentionNewsCount": len(mention_items),
    }


def should_add_news_panel_without_layout(props: dict[str, Any] | None, *, include_empty: bool = False) -> bool:
    if not props:
        return False
    has_content = bool(props.get("latestNews") or props.get("majorNews") or props.get("dailySummaries"))
    return has_content or bool(include_empty and props.get("status") == "empty")


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


def is_direct_news_item(item: EvidenceItem) -> bool:
    if item.provider != "news" or item.status != "available":
        return False
    raw = item.raw if isinstance(item.raw, dict) else {}
    level = str(raw.get("subjectRelevance") or "").strip().lower()
    return level not in {"mention", "irrelevant"}


def is_mention_news_item(item: EvidenceItem) -> bool:
    if item.provider != "news" or item.status != "available":
        return False
    raw = item.raw if isinstance(item.raw, dict) else {}
    return str(raw.get("subjectRelevance") or "").strip().lower() == "mention"


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
    }


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
