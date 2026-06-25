from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID


class OrderContractError(ValueError):
    """Raised when a Kafka order command violates the public contract."""


class OrderStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    REJECTED = "REJECTED"
    SUBMIT_FAILED_UNKNOWN = "SUBMIT_FAILED_UNKNOWN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.RECEIVED: {OrderStatus.VALIDATED, OrderStatus.REJECTED},
    OrderStatus.VALIDATED: {OrderStatus.SUBMITTING},
    OrderStatus.SUBMITTING: {
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,
        OrderStatus.SUBMIT_FAILED_UNKNOWN,
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
    },
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.FILLED, OrderStatus.CANCELED},
    OrderStatus.SUBMIT_FAILED_UNKNOWN: {
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,
        OrderStatus.RECONCILIATION_REQUIRED,
    },
    OrderStatus.REJECTED: set(),
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.RECONCILIATION_REQUIRED: set(),
}


FORBIDDEN_FIELD_NAMES = {
    "account_no",
    "account_number",
    "access_token",
    "appkey",
    "appsecret",
    "authorization",
    "cano",
    "kis_account_no",
    "kis_demo_account_no",
    "kis_real_account_no",
    "raw_idempotency_key",
    "token",
}


@dataclass(frozen=True)
class OrderCommand:
    market: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    exchange: str
    order_division: str
    sell_type: str = ""
    condition_price: str = ""

    @property
    def kafka_symbol_key(self) -> str:
        return self.symbol

    def to_payload(self) -> dict[str, str]:
        payload = {
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "qty": format(self.qty, "f"),
            "price": format(self.price, "f"),
            "exchange": self.exchange,
            "order_division": self.order_division,
        }
        if self.market == "domestic":
            payload["sell_type"] = self.sell_type
            payload["condition_price"] = self.condition_price
        return payload


@dataclass(frozen=True)
class OrderCommandEnvelope:
    schema_version: int
    event_type: str
    event_id: str
    request_id: str
    occurred_at: str
    producer: str
    env: str
    account_alias: str
    payload: dict[str, Any]
    command: OrderCommand

    @property
    def kafka_key(self) -> str:
        return build_order_key(self.account_alias, self.command.kafka_symbol_key)


def build_order_key(account_alias: str, symbol: str) -> str:
    return f"{account_alias}:{symbol}"


def assert_transition_allowed(current: OrderStatus, next_status: OrderStatus) -> None:
    if current == next_status:
        return
    if next_status not in ALLOWED_TRANSITIONS[current]:
        raise OrderContractError(f"Invalid order status transition: {current} -> {next_status}")


def validate_order_command_message(value: dict[str, Any], *, key: str | None = None) -> OrderCommandEnvelope:
    if not isinstance(value, dict):
        raise OrderContractError("Kafka message value must be a JSON object.")
    _assert_no_forbidden_fields(value)

    schema_version = _require_int(value, "schema_version")
    if schema_version != 1:
        raise OrderContractError("schema_version must be 1.")

    event_type = _require_str(value, "event_type")
    if event_type != "order.submit.requested":
        raise OrderContractError("event_type must be order.submit.requested.")

    event_id = _require_uuid_str(value, "event_id")
    request_id = _require_uuid_str(value, "request_id")
    occurred_at = _require_str(value, "occurred_at")
    producer = _require_str(value, "producer")
    env = _require_str(value, "env")
    if env not in {"demo", "real"}:
        raise OrderContractError("env must be demo or real.")
    account_alias = _require_str(value, "account_alias")

    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise OrderContractError("payload must be an object.")
    command = _validate_payload(payload)

    envelope = OrderCommandEnvelope(
        schema_version=schema_version,
        event_type=event_type,
        event_id=event_id,
        request_id=request_id,
        occurred_at=occurred_at,
        producer=producer,
        env=env,
        account_alias=account_alias,
        payload=payload,
        command=command,
    )
    if key is not None and key != envelope.kafka_key:
        raise OrderContractError(f"Kafka key must be {envelope.kafka_key!r}.")
    return envelope


def _validate_payload(payload: dict[str, Any]) -> OrderCommand:
    market = _require_str(payload, "market")
    if market not in {"domestic", "overseas"}:
        raise OrderContractError("payload.market must be domestic or overseas.")

    symbol = _require_str(payload, "symbol").upper()
    side = _require_str(payload, "side")
    if side not in {"buy", "sell"}:
        raise OrderContractError("payload.side must be buy or sell.")
    qty = _require_decimal(payload, "qty")
    if qty <= 0:
        raise OrderContractError("payload.qty must be greater than zero.")
    price = _require_decimal(payload, "price")
    if price < 0:
        raise OrderContractError("payload.price must be zero or greater.")
    exchange = _require_str(payload, "exchange").upper()
    order_division = _require_str(payload, "order_division")

    sell_type = str(payload.get("sell_type", "")).strip()
    condition_price = str(payload.get("condition_price", "")).strip()
    if market == "domestic" and (not symbol.isdigit() or len(symbol) not in {6, 7}):
        raise OrderContractError("domestic symbol must be a 6-digit stock code or 7-digit ETN code.")

    return OrderCommand(
        market=market,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        exchange=exchange,
        order_division=order_division,
        sell_type=sell_type,
        condition_price=condition_price,
    )


def _assert_no_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_FIELD_NAMES:
                raise OrderContractError(f"Forbidden sensitive field in Kafka message: {key}")
            _assert_no_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_fields(item)


def _require_str(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise OrderContractError(f"{field} is required.")
    return item.strip()


def _require_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int):
        raise OrderContractError(f"{field} must be an integer.")
    return item


def _require_decimal(value: dict[str, Any], field: str) -> Decimal:
    item = value.get(field)
    if not isinstance(item, str):
        raise OrderContractError(f"payload.{field} must be a numeric string.")
    try:
        return Decimal(item)
    except InvalidOperation as exc:
        raise OrderContractError(f"payload.{field} must be a numeric string.") from exc


def _require_uuid_str(value: dict[str, Any], field: str) -> str:
    item = _require_str(value, field)
    try:
        UUID(item)
    except ValueError as exc:
        raise OrderContractError(f"{field} must be a UUID string.") from exc
    return item
