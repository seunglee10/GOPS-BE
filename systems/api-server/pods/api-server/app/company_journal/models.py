from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


CONTRACT_VERSION = "company-journal.v2"
PROMPT_VERSION = "company-journal-ko.v2"
REQUIRED_TABS = ("current", "growth", "profitability", "earnings", "stability", "valuation")


@dataclass(frozen=True)
class GenerationRequest:
    request_id: str
    symbol: str
    analysis_as_of: date
    input_digest: str
    requested_source: str


@dataclass(frozen=True)
class NarrativeDraft:
    headline: str
    keywords: list[str]
    recent_movement: str
    financial_stability: str
    watch_items: str
    tabs: dict[str, str]
    model: str


def validate_narrative(draft: NarrativeDraft) -> list[str]:
    errors: list[str] = []
    for field, value in (
        ("headline", draft.headline),
        ("recent_movement", draft.recent_movement),
        ("financial_stability", draft.financial_stability),
        ("watch_items", draft.watch_items),
    ):
        if not value.strip():
            errors.append(f"{field} is empty")
    if not 1 <= len(draft.keywords) <= 5:
        errors.append("keywords must contain 1 to 5 items")
    if any(not keyword.strip() for keyword in draft.keywords):
        errors.append("keywords contain an empty item")
    for tab in REQUIRED_TABS:
        if not str(draft.tabs.get(tab) or "").strip():
            errors.append(f"tabs.{tab} is empty")
    return errors


def report_payload(row: dict[str, Any]) -> dict[str, Any]:
    import json

    def decode(value: Any, fallback: Any) -> Any:
        if isinstance(value, type(fallback)):
            return value
        try:
            return json.loads(value or "")
        except (TypeError, ValueError):
            return fallback

    return {
        "contractVersion": row.get("contract_version") or CONTRACT_VERSION,
        "symbol": row.get("symbol"),
        "analysisAsOf": row.get("analysis_as_of"),
        "generatedAt": row.get("generated_at"),
        "inputDigest": row.get("input_digest"),
        "headline": row.get("headline") or "",
        "keywords": list(row.get("keywords") or []),
        "recentMovement": row.get("recent_movement") or "",
        "financialStability": row.get("financial_stability") or "",
        "watchItems": row.get("watch_items") or "",
        "tabs": decode(row.get("tab_narratives_json"), {}),
        "serverMetrics": decode(row.get("server_metrics_json"), {}),
        "sourceReceipt": decode(row.get("source_receipt_json"), {}),
        "missingData": list(row.get("missing_data") or []),
        "validationStatus": row.get("validation_status") or "unknown",
    }
