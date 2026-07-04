from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


@dataclass
class EvidenceItem:
    provider: str
    status: str
    title: str
    summary: str
    observedAt: str = field(default_factory=utc_now_iso)
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def no_data(cls, provider: str, title: str, summary: str) -> "EvidenceItem":
        return cls(provider=provider, status="no-data", title=title, summary=summary)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentFinding:
    agentId: str
    role: str
    summary: str
    rationale: str
    confidence: float = 0.5
    evidence: list[EvidenceItem] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data


@dataclass
class IntentRoute:
    source: str
    intentType: str
    selectedRoles: list[str]
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimePolicy:
    max_realtime_llm_calls: int = 1
    default_route_strategy: str = "rule_search_cache"
    route_llm_fallback: str = "degraded_only"
    route_llm_fallback_threshold: float = 0.75
    llm_guardrail: str = "degraded_only"
    total_timeout_ms: int = 3000
    snapshot_timeout_ms: int = 700
    synthesis_timeout_ms: int = 1700
    graphdb_timeout_ms: int = 500
    max_items_per_snapshot: int = 5
    max_total_synthesis_evidence_items: int = 15
    max_synthesis_output_tokens: int = 350
    stream_synthesis_response: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoutePlan:
    run_id: str
    intent: str
    route_confidence: float
    entity_candidates: list[str] = field(default_factory=list)
    snapshot_bundle: list[str] = field(default_factory=list)
    execution_mode: str = "parallel_snapshots"
    llm_calls_allowed: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResolvedEntity:
    raw_name: str
    canonical_name: str
    ticker: str | None = None
    market: str = "US"
    asset_type: str = "stock"
    graph_node_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentSignal:
    target: str
    direction: str = "unknown"
    horizon: str = "unknown"
    strength: str = "low"
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataSnapshot:
    snapshot_id: str
    run_id: str
    snapshot_type: str
    status: str
    source: str
    cache_hit: bool
    freshness: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    signals: list[AgentSignal] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    data_quality: str = "low"
    confidence: float = 0.5
    latency_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signals"] = [item.to_dict() for item in self.signals]
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data


@dataclass
class SynthesisInput:
    run_id: str
    original_prompt: str
    intent: str
    entities: list[ResolvedEntity] = field(default_factory=list)
    snapshots: list[DataSnapshot] = field(default_factory=list)
    crossSignals: list[dict[str, Any]] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    output_policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["entities"] = [item.to_dict() for item in self.entities]
        data["snapshots"] = [item.to_dict() for item in self.snapshots]
        return data


@dataclass
class FinalResponse:
    run_id: str
    answer_type: str
    summary: str
    key_points: list[str] = field(default_factory=list)
    bullish_points: list[str] = field(default_factory=list)
    bearish_points: list[str] = field(default_factory=list)
    relationship_impacts: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    data_freshness_warnings: list[str] = field(default_factory=list)
    partial_data_used: bool = False
    confidence: float = 0.5
    final_stance: str = "not_applicable"
    latency_ms: float = 0.0
    llm_calls_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LatencyStage:
    stage: str
    latency_ms: float
    status: str
    cache_hit: bool | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class LatencyTrace:
    run_id: str
    total_latency_ms: float = 0.0
    llm_calls_used: int = 0
    stages: list[LatencyStage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = [item.to_dict() for item in self.stages]
        return data


@dataclass
class FinalAnswerCitation:
    provider: str
    title: str
    url: str | None = None
    publishedAt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinalAnswerSection:
    title: str
    bullets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinalAnswer:
    title: str
    summary: str
    sections: list[FinalAnswerSection] = field(default_factory=list)
    citations: list[FinalAnswerCitation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sections"] = [item.to_dict() for item in self.sections]
        data["citations"] = [item.to_dict() for item in self.citations]
        return data


@dataclass
class AgentAnswer:
    agentId: str
    role: str
    title: str
    content: str
    confidence: float = 0.5
    citations: list[FinalAnswerCitation] = field(default_factory=list)
    createdAt: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["citations"] = [item.to_dict() for item in self.citations]
        return data


@dataclass
class MarketEvent:
    eventId: str
    symbol: str
    eventType: str
    severity: str
    sourceTopic: str
    observedAt: str
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)

    @classmethod
    def from_payload(
        cls,
        *,
        symbol: str,
        event_type: str,
        severity: str,
        source_topic: str,
        summary: str,
        metrics: dict[str, Any],
        observed_at: str | None = None,
    ) -> "MarketEvent":
        observed_at = observed_at or utc_now_iso()
        event_id = stable_id(
            "market-event",
            {
                "symbol": symbol,
                "eventType": event_type,
                "sourceTopic": source_topic,
                "observedAt": observed_at,
                "metrics": metrics,
            },
        )
        evidence = [
            EvidenceItem(
                provider="market-data",
                status="available",
                title=event_type,
                summary=summary,
                observedAt=observed_at,
                raw=metrics,
            )
        ]
        return cls(
            eventId=event_id,
            symbol=symbol,
            eventType=event_type,
            severity=severity,
            sourceTopic=source_topic,
            observedAt=observed_at,
            summary=summary,
            metrics=metrics,
            evidence=evidence,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketEvent":
        evidence = [
            item if isinstance(item, EvidenceItem) else EvidenceItem(**item)
            for item in data.get("evidence", [])
            if isinstance(item, (dict, EvidenceItem))
        ]
        return cls(
            eventId=str(data.get("eventId") or data.get("event_id") or stable_id("market-event", data)),
            symbol=str(data.get("symbol") or "UNKNOWN").upper(),
            eventType=str(data.get("eventType") or data.get("event_type") or "unknown"),
            severity=str(data.get("severity") or "info"),
            sourceTopic=str(data.get("sourceTopic") or data.get("source_topic") or "manual"),
            observedAt=str(data.get("observedAt") or data.get("observed_at") or utc_now_iso()),
            summary=str(data.get("summary") or "Market event detected."),
            metrics=dict(data.get("metrics") or {}),
            evidence=evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data


@dataclass
class LayoutProposal:
    title: str
    rationale: str
    commands: list[dict[str, Any]] = field(default_factory=list)
    autoApply: bool = True
    panelPriorities: list[dict[str, Any]] = field(default_factory=list)
    createdAt: str = field(default_factory=utc_now_iso)
    id: str = field(default_factory=lambda: stable_id("layout-proposal", {"createdAt": utc_now_iso()}))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NotificationDecision:
    decisionId: str
    analysisId: str
    symbol: str
    level: str
    showToast: bool
    title: str
    message: str
    reason: str
    eventId: str | None = None
    createdAt: str = field(default_factory=utc_now_iso)
    expiresAt: str | None = None
    channels: list[str] = field(default_factory=lambda: ["websocket", "redis"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisReport:
    analysisId: str
    symbol: str
    intent: str
    status: str
    createdAt: str
    summary: str
    rationale: str
    findings: list[AgentFinding] = field(default_factory=list)
    marketEvents: list[MarketEvent] = field(default_factory=list)
    providerEvidence: list[EvidenceItem] = field(default_factory=list)
    route: IntentRoute | None = None
    finalAnswer: FinalAnswer | None = None
    notificationDecision: NotificationDecision | None = None
    layoutProposal: LayoutProposal | None = None
    chartProposal: dict[str, Any] | None = None
    dailySummaries: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    routePlan: RoutePlan | None = None
    resolvedEntities: list[ResolvedEntity] = field(default_factory=list)
    snapshots: list[DataSnapshot] = field(default_factory=list)
    synthesisInput: SynthesisInput | None = None
    finalResponse: FinalResponse | None = None
    latencyTrace: LatencyTrace | None = None
    agentAnswers: list[AgentAnswer] = field(default_factory=list)
    agentTrace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["findings"] = [item.to_dict() for item in self.findings]
        data["marketEvents"] = [item.to_dict() for item in self.marketEvents]
        data["providerEvidence"] = [item.to_dict() for item in self.providerEvidence]
        data["route"] = self.route.to_dict() if self.route else None
        data["finalAnswer"] = self.finalAnswer.to_dict() if self.finalAnswer else None
        data["notificationDecision"] = self.notificationDecision.to_dict() if self.notificationDecision else None
        data["layoutProposal"] = self.layoutProposal.to_dict() if self.layoutProposal else None
        data["routePlan"] = self.routePlan.to_dict() if self.routePlan else None
        data["resolvedEntities"] = [item.to_dict() for item in self.resolvedEntities]
        data["snapshots"] = [item.to_dict() for item in self.snapshots]
        data["synthesisInput"] = self.synthesisInput.to_dict() if self.synthesisInput else None
        data["finalResponse"] = self.finalResponse.to_dict() if self.finalResponse else None
        data["latencyTrace"] = self.latencyTrace.to_dict() if self.latencyTrace else None
        data["agentAnswers"] = [item.to_dict() for item in self.agentAnswers]
        from ..security import sanitize_value

        return sanitize_value(data).value
