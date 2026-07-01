"""Order command and request models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .status import OrderContractError

SUPPORTED_ORDER_MARKETS = frozenset({"overseas"})
SUPPORTED_ORDER_DIVISIONS = frozenset({"00"})
SUPPORTED_OVERSEAS_EXCHANGES = frozenset({"NASD", "NYSE", "AMEX", "SEHK", "SHAA", "SZAA", "TKSE", "HASE", "VNSE"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OrderRequest:
    account_alias: str
    market: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    exchange: str
    order_division: str
    actor_id: str | None = None
    role: str | None = None
    sell_type: str | None = None
    condition_price: str | None = None

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "price": str(self.price),
            "exchange": self.exchange,
            "order_division": self.order_division,
        }
        if self.sell_type is not None:
            payload["sell_type"] = self.sell_type
        if self.condition_price is not None:
            payload["condition_price"] = self.condition_price
        return payload


@dataclass(frozen=True)
class OrderCommand:
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
    market: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    exchange: str
    order_division: str
    payload: dict[str, Any] = field(default_factory=dict)
    actor_id: str | None = None
    role: str | None = None
    broker_order_id: str | None = None

    def to_envelope(self) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "request_id": self.request_id,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "account_alias": self.account_alias,
            "occurred_at": self.occurred_at,
            "producer": self.producer,
            "env": self.env,
            "source": self.source,
            "payload": _json_ready(self.payload),
        }
        if self.actor_id is not None:
            envelope["actor_id"] = self.actor_id
        if self.role is not None:
            envelope["role"] = self.role
        if self.broker_order_id is not None:
            envelope["broker_order_id"] = self.broker_order_id
        return envelope


def validate_order_request_payload(payload: dict[str, Any], *, default_account_alias: str) -> OrderRequest:
    if not isinstance(payload, dict):
        raise OrderContractError("order request must be a JSON object")

    account_alias = _non_empty(payload.get("account_alias") or default_account_alias, "account_alias")
    market = _one_of(payload.get("market"), set(SUPPORTED_ORDER_MARKETS), "market")
    side = _one_of(payload.get("side"), {"buy", "sell"}, "side")
    qty = _positive_integer_decimal(payload.get("qty"), "qty")
    price = _positive_decimal(payload.get("price"), "price")
    symbol = _non_empty(payload.get("symbol"), "symbol").upper()
    exchange = _non_empty(payload.get("exchange"), "exchange").upper()
    order_division = _non_empty(payload.get("order_division", "00"), "order_division")
    if exchange not in SUPPORTED_OVERSEAS_EXCHANGES:
        raise OrderContractError(f"exchange must be one of {sorted(SUPPORTED_OVERSEAS_EXCHANGES)}")
    if order_division not in SUPPORTED_ORDER_DIVISIONS:
        raise OrderContractError("order_division must be '00' for KIS overseas demo limit orders")

    return OrderRequest(
        account_alias=account_alias,
        market=market,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        exchange=exchange,
        order_division=order_division,
        actor_id=_optional_string(payload.get("actor_id")),
        role=_optional_string(payload.get("role")),
        sell_type=_optional_string(payload.get("sell_type")),
        condition_price=_optional_string(payload.get("condition_price")),
    )


def order_command_from_envelope(message: dict[str, Any]) -> OrderCommand:
    required_envelope = [
        "schema_version",
        "event_type",
        "event_id",
        "request_id",
        "order_id",
        "client_order_id",
        "account_alias",
        "occurred_at",
        "producer",
        "env",
        "source",
        "payload",
    ]
    for field_name in required_envelope:
        if field_name not in message:
            raise OrderContractError(f"missing envelope field: {field_name}")
    if message["schema_version"] != 1:
        raise OrderContractError("unsupported schema_version")
    payload = message["payload"]
    if not isinstance(payload, dict):
        raise OrderContractError("payload must be a JSON object")

    request = validate_order_request_payload(payload, default_account_alias=str(message["account_alias"]))
    return OrderCommand(
        schema_version=1,
        event_type=_non_empty(message["event_type"], "event_type"),
        event_id=_non_empty(message["event_id"], "event_id"),
        request_id=_non_empty(message["request_id"], "request_id"),
        order_id=_non_empty(message["order_id"], "order_id"),
        client_order_id=_non_empty(message["client_order_id"], "client_order_id"),
        account_alias=_non_empty(message["account_alias"], "account_alias"),
        occurred_at=_non_empty(message["occurred_at"], "occurred_at"),
        producer=_non_empty(message["producer"], "producer"),
        env=_one_of(message["env"], {"demo"}, "env"),
        source=_non_empty(message["source"], "source"),
        market=request.market,
        symbol=request.symbol,
        side=request.side,
        qty=request.qty,
        price=request.price,
        exchange=request.exchange,
        order_division=request.order_division,
        payload=request.payload(),
        actor_id=_optional_string(message.get("actor_id")),
        role=_optional_string(message.get("role")),
        broker_order_id=_optional_string(message.get("broker_order_id")),
    )


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderContractError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _one_of(value: Any, allowed: set[str], field: str) -> str:
    normalized = _non_empty(value, field).lower()
    if normalized not in allowed:
        raise OrderContractError(f"{field} must be one of {sorted(allowed)}")
    return normalized


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderContractError(f"{field} must be a decimal") from exc
    if parsed <= 0:
        raise OrderContractError(f"{field} must be positive")
    return parsed


def _positive_integer_decimal(value: Any, field: str) -> Decimal:
    parsed = _positive_decimal(value, field)
    integral = parsed.to_integral_value()
    if parsed != integral:
        raise OrderContractError(f"{field} must be a whole-share quantity")
    return integral


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_ready(child) for child in value]
    return value
