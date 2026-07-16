"""Canonical real-fill observations shared by Postgres and memory repositories."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def canonical_fill_observation(
    order: dict[str, Any],
    payload: dict[str, Any] | None,
    *,
    execution_id: str | None,
    observed_at: datetime | None = None,
) -> dict[str, Any] | None:
    payload = payload or {}
    user_sub = str(order.get("user_sub") or "").strip()
    order_id = str(order.get("order_id") or "").strip()
    symbol = str(order.get("symbol") or payload.get("symbol") or "").strip().upper()
    side = str(order.get("side") or payload.get("side") or "").strip().lower()
    quantity = _positive_decimal(
        payload.get("cumulative_filled_qty")
        or payload.get("cumulativeFilledQty")
        or payload.get("filled_qty")
        or payload.get("filledQty")
    )
    price = _positive_decimal(
        payload.get("average_fill_price")
        or payload.get("averageFillPrice")
        or payload.get("avg_fill_price")
        or payload.get("fill_price")
        or payload.get("execution_price")
        or payload.get("price")
    )
    if not user_sub or not order_id or not symbol or side not in {"buy", "sell"} or quantity is None or price is None:
        return None

    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    filled_at = _parse_datetime(
        payload.get("filled_at")
        or payload.get("filledAt")
        or payload.get("executed_at")
        or payload.get("executedAt")
        or payload.get("occurred_at")
    ) or observed
    if filled_at > observed:
        filled_at = observed
    decision_at = _parse_datetime(order.get("occurred_at"))
    if decision_at is None or decision_at > filled_at:
        decision_at = filled_at
    status = str(payload.get("status") or order.get("status") or "FILLED").strip().upper()
    digest_material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return {
        "fill_id": f"kis:{order_id}",
        "user_sub": user_sub,
        "order_id": order_id,
        "source_execution_id": execution_id,
        "symbol": symbol,
        "side": side,
        "cumulative_filled_qty": quantity,
        "average_fill_price": price,
        "status": status,
        "decision_at": decision_at,
        "filled_at": filled_at,
        "source_observed_at": observed,
        "source_payload_digest": hashlib.sha256(digest_material).hexdigest(),
    }


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
