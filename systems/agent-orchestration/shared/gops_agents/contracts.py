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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["findings"] = [item.to_dict() for item in self.findings]
        data["marketEvents"] = [item.to_dict() for item in self.marketEvents]
        data["providerEvidence"] = [item.to_dict() for item in self.providerEvidence]
        data["route"] = self.route.to_dict() if self.route else None
        data["finalAnswer"] = self.finalAnswer.to_dict() if self.finalAnswer else None
        data["notificationDecision"] = self.notificationDecision.to_dict() if self.notificationDecision else None
        data["layoutProposal"] = self.layoutProposal.to_dict() if self.layoutProposal else None
        return data
