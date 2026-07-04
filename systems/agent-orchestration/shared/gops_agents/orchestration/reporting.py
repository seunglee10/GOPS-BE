from __future__ import annotations

from typing import Any

from ..contracts import EvidenceItem, MarketEvent
from ..retrieval.context import RetrievalContext
from ..retrieval.cross_signal import CrossSignal
from ..roles import AgentContext


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


def build_agent_trace(
    snapshots,
    retrieval_context: RetrievalContext | None = None,
    cross_signals: list[CrossSignal] | None = None,
    entity_resolution: dict[str, Any] | None = None,
    query_understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    if entity_resolution:
        trace["entityResolution"] = dict(entity_resolution)
    if query_understanding:
        trace["queryUnderstanding"] = dict(query_understanding)
    return trace


def build_summary(symbol: str, findings, events: list[MarketEvent]) -> str:
    if events:
        strongest = max(events, key=lambda event: {"info": 1, "watch": 2, "alert": 3, "critical": 4}.get(event.severity, 1))
        return f"{symbol} has a {strongest.severity} {strongest.eventType} signal."
    return f"{symbol} multi-agent analysis completed."
