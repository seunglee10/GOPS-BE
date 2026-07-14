from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def match_quote_payload(repository, payload: Any, *, fallback_event_id: str | None = None):
    if not isinstance(payload, dict) or str(payload.get("eventType") or "").upper() != "QUOTE":
        return []
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        return []
    return repository.match_quote(
        symbol=symbol,
        bid_price=optional_positive_decimal(payload.get("bidPrice")),
        ask_price=optional_positive_decimal(payload.get("askPrice")),
        quote_timestamp=str(payload.get("timestamp") or payload.get("receivedAt") or "") or None,
        quote_event_id=str(payload.get("sourceEventId") or "").strip() or fallback_event_id,
    )


def optional_positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
