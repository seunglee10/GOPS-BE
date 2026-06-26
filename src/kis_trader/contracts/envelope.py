from __future__ import annotations

from typing import Any
from uuid import uuid4

from .order import OrderCommand, now_utc_iso
from .redaction import SENSITIVE_KEYS, redact_sensitive
from .statuses import OrderStatus


class ContractError(ValueError):
    """Raised when an event violates the public order contract."""


def build_order_command_envelope(
    *,
    command: OrderCommand,
    env: str,
    account_alias: str,
    order_id: str,
    request_id: str,
    client_order_id: str,
    event_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "event_type": "order.submit.requested",
        "event_id": event_id or str(uuid4()),
        "request_id": request_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "account_alias": account_alias,
        "occurred_at": now_utc_iso(),
        "producer": "backend-api",
        "env": env,
        "source": "order-api",
        "payload": command.to_payload(),
    }
    ensure_no_forbidden_fields(payload)
    return payload


def build_submit_result_envelope(
    *,
    env: str,
    account_alias: str,
    order_id: str,
    request_id: str,
    client_order_id: str,
    status: OrderStatus,
    market: str,
    symbol: str,
    side: str,
    qty: str,
    price: str,
    exchange: str,
    kis_order_id: str | None,
    kis_msg_cd: str | None,
    error_type: str | None,
    event_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "event_type": _event_type_for_status(status),
        "event_id": event_id or str(uuid4()),
        "request_id": request_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "account_alias": account_alias,
        "occurred_at": now_utc_iso(),
        "producer": "kis-broker-adapter",
        "env": env,
        "source": "kis-broker-adapter",
        "status": status.value,
        "payload": {
            "market": market,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "exchange": exchange,
            "kis_order_id": kis_order_id,
            "kis_msg_cd": kis_msg_cd,
            "error_type": error_type,
        },
    }
    redacted = redact_sensitive(payload)
    ensure_no_forbidden_fields(redacted)
    return redacted


def ensure_no_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_KEYS:
                raise ContractError(f"Forbidden sensitive field in message: {key}")
            ensure_no_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            ensure_no_forbidden_fields(item)


def _event_type_for_status(status: OrderStatus) -> str:
    event_types = {
        OrderStatus.SUBMITTED: "order.submitted",
        OrderStatus.REJECTED: "order.rejected",
        OrderStatus.RISK_REJECTED: "order.risk_rejected",
        OrderStatus.SUBMIT_FAILED_UNKNOWN: "order.submit_failed_unknown",
        OrderStatus.FAILED: "order.failed",
    }
    try:
        return event_types[status]
    except KeyError as exc:
        raise ContractError(f"{status.value} is not a submit result status.") from exc
