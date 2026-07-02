from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .contracts import DataSnapshot, EvidenceItem, stable_id
from .retrieval_context import RetrievalContext


@dataclass
class CrossSignal:
    target_symbol: str
    signal_type: str
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.5
    explanation: str = ""
    related_symbol: str | None = None
    theme: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def build_cross_signals(
    *,
    primary_symbol: str,
    snapshots: list[DataSnapshot],
    retrieval_context: RetrievalContext | None,
) -> list[CrossSignal]:
    primary = str(primary_symbol or "UNKNOWN").upper()
    related_symbols = set(retrieval_context.related_symbol_values() if retrieval_context is not None else [])
    signals: list[CrossSignal] = []
    news_snapshot = snapshot_by_type(snapshots, "news_snapshot")
    relationship_snapshot = snapshot_by_type(snapshots, "relationship_snapshot")
    market_snapshot = snapshot_by_type(snapshots, "market_snapshot")

    if news_snapshot is not None:
        for item in available_evidence(news_snapshot):
            symbol = evidence_symbol(item, primary)
            ref = evidence_ref(item)
            if symbol in related_symbols:
                signals.append(CrossSignal(
                    target_symbol=primary,
                    related_symbol=symbol,
                    signal_type="peer-confirmed",
                    evidence_refs=[ref],
                    confidence=min(0.82, max(0.55, news_snapshot.confidence)),
                    explanation=f"{symbol} related news appears inside the bounded graph-aware retrieval set.",
                ))
            elif symbol == primary:
                signals.append(CrossSignal(
                    target_symbol=primary,
                    signal_type="single-name",
                    evidence_refs=[ref],
                    confidence=min(0.78, max(0.5, news_snapshot.confidence)),
                    explanation=f"{primary} has direct news evidence in the current retrieval window.",
                ))

    if relationship_snapshot is not None:
        for item in available_evidence(relationship_snapshot):
            theme = evidence_theme(item)
            if theme:
                signals.append(CrossSignal(
                    target_symbol=primary,
                    theme=theme,
                    signal_type="theme-wide",
                    evidence_refs=[evidence_ref(item)],
                    confidence=min(0.76, max(0.48, relationship_snapshot.confidence)),
                    explanation=f"Graph relationship evidence maps {primary} to {theme}.",
                ))

    if not signals and market_snapshot is not None and market_snapshot.status in {"success", "partial"}:
        refs = [evidence_ref(item) for item in market_snapshot.evidence[:2]]
        signals.append(CrossSignal(
            target_symbol=primary,
            signal_type="unconfirmed",
            evidence_refs=refs,
            confidence=min(0.5, market_snapshot.confidence),
            explanation=f"{primary} has market context but no confirmed cross-source relation signal.",
        ))

    return dedupe_cross_signals(signals)[:6]


def snapshot_by_type(snapshots: list[DataSnapshot], snapshot_type: str) -> DataSnapshot | None:
    return next((snapshot for snapshot in snapshots if snapshot.snapshot_type == snapshot_type), None)


def available_evidence(snapshot: DataSnapshot) -> list[EvidenceItem]:
    return [item for item in snapshot.evidence if item.status == "available"]


def evidence_symbol(item: EvidenceItem, fallback: str) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get("targetSymbol") or raw.get("symbol") or fallback
    return str(value or fallback).strip().upper()


def evidence_theme(item: EvidenceItem) -> str | None:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get("themeName") or raw.get("theme") or raw.get("theme_name")
    text = str(value or "").strip()
    return text or None


def evidence_ref(item: EvidenceItem) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    article_id = str(raw.get("articleId") or raw.get("article_id") or "").strip()
    if article_id:
        return f"news:{article_id}"
    return stable_id("evidence", {
        "provider": item.provider,
        "title": item.title,
        "url": item.url,
        "observedAt": item.observedAt,
    })


def dedupe_cross_signals(signals: list[CrossSignal]) -> list[CrossSignal]:
    deduped: dict[tuple[str, str | None, str | None, str], CrossSignal] = {}
    for signal in signals:
        key = (signal.target_symbol, signal.related_symbol, signal.theme, signal.signal_type)
        current = deduped.get(key)
        if current is None or signal.confidence > current.confidence:
            deduped[key] = signal
    return sorted(deduped.values(), key=lambda item: item.confidence, reverse=True)
