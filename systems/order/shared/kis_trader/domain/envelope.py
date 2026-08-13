"""Kafka envelope validation and construction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from uuid import UUID

from .commands import OrderCommand, OrderRequest, order_command_from_envelope
from .status import OrderContractError


@dataclass(frozen=True)
class KafkaEnvelope:
    schema_version: int
    event_type: str
    event_id: str
    request_id: str
    order_id: str
    client_order_id: str
    account_alias: str
    occurred_at: str
    producer: str
    env: str
    source: str
    payload: dict[str, Any]
    actor_id: str | None = None
    role: str | None = None


def build_order_command_envelope(
    request: OrderRequest,
    *,
    occurred_at: str,
    request_id: str | None = None,
    order_id: str | None = None,
    client_order_id: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    command = OrderCommand(
        schema_version=1,
        event_type="order.submit.requested",
        event_id=event_id or f"evt_{uuid4().hex}",
        request_id=request_id or f"req_{uuid4().hex}",
        order_id=order_id or f"ord_{uuid4().hex}",
        client_order_id=client_order_id or f"coid_{uuid4().hex}",
        account_alias=request.account_alias,
        occurred_at=occurred_at,
        producer="backend-api",
        env="demo",
        source="order-api",
        market=request.market,
        symbol=request.symbol,
        side=request.side,
        qty=request.qty,
        price=request.price,
        exchange=request.exchange,
        order_division=request.order_division,
        payload=request.payload(),
        actor_id=request.actor_id,
        role=request.role,
    )
    return command.to_envelope()


def build_order_status_envelope(
    order: dict[str, Any],
    *,
    event_type: str,
    producer: str,
    source: str,
    payload: dict[str, Any],
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    envelope = {
        "schema_version": 1,
        "event_type": event_type,
        "event_id": event_id or f"evt_{uuid4().hex}",
        "request_id": _required_order_field(order, "request_id"),
        "order_id": _required_order_field(order, "order_id"),
        "client_order_id": _required_order_field(order, "client_order_id"),
        "account_alias": _required_order_field(order, "account_alias"),
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "producer": producer,
        "env": "demo",
        "source": source,
        "payload": payload,
    }
    return enrich_order_envelope_identity(envelope, order)


def enrich_order_envelope_identity(envelope: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(envelope)
    user_sub = str(order.get("user_sub") or "").strip()
    symbol = str(order.get("symbol") or (enriched.get("payload") or {}).get("symbol") or "").strip()
    if user_sub:
        enriched["user_sub"] = user_sub
        enriched["app_user_id"] = str(
            order.get("app_user_id") or _deterministic_uuid("gops-app-user", user_sub)
        )
    if symbol:
        canonical_symbol = symbol.upper().replace("-", ".")
        enriched["instrument_id"] = str(
            order.get("instrument_id") or _deterministic_uuid("gops-instrument", canonical_symbol)
        )
    return enriched


def _deterministic_uuid(namespace: str, value: str) -> UUID:
    digest = hashlib.md5(f"{namespace}:{value}".encode("utf-8"), usedforsecurity=False).hexdigest()
    return UUID(digest)


def build_order_fill_envelope(order: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    """Fill event for orders.fills.v1 — consumed by the risk monitor.

    Carries the trade facts (symbol/side/qty/price) that the plain status
    envelope omits, so downstream consumers can update position state without
    a database lookup.
    """
    return build_order_status_envelope(
        order,
        event_type="order.filled",
        producer="kis-trader-order-status",
        source="orders.fills.v1",
        payload={
            "symbol": _required_order_field(order, "symbol"),
            "side": _required_order_field(order, "side"),
            "qty": _required_numeric_text(order, "qty"),
            "price": _required_numeric_text(order, "price"),
            "status": _required_order_field(order, "status"),
            "reason": reason,
        },
    )


def _required_numeric_text(order: dict[str, Any], field_name: str) -> str:
    value = order.get(field_name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise OrderContractError(f"missing order field for envelope: {field_name}")
    return str(value).strip()


def validate_order_envelope(message: dict[str, Any]) -> OrderCommand:
    if not isinstance(message, dict):
        raise OrderContractError("Kafka envelope must be a JSON object")
    return order_command_from_envelope(message)


def _required_order_field(order: dict[str, Any], field_name: str) -> str:
    value = order.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise OrderContractError(f"missing order field for envelope: {field_name}")
    return value
