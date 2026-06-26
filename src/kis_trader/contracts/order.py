from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .statuses import OrderStatus


class OrderValidationError(ValueError):
    """Raised when a request cannot become a canonical order command."""


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

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "OrderCommand":
        market = _required_str(value, "market").lower()
        if market not in {"domestic", "overseas"}:
            raise OrderValidationError("market must be domestic or overseas.")

        symbol = _required_str(value, "symbol").upper()
        if market == "domestic" and (not symbol.isdigit() or len(symbol) not in {6, 7}):
            raise OrderValidationError("domestic symbol must be a 6-digit stock code or 7-digit ETN code.")

        side = _required_str(value, "side").lower()
        if side not in {"buy", "sell"}:
            raise OrderValidationError("side must be buy or sell.")

        qty = _decimal_field(value, "qty")
        if qty <= 0:
            raise OrderValidationError("qty must be greater than zero.")

        price = _decimal_field(value, "price")
        if price < 0:
            raise OrderValidationError("price must be zero or greater.")

        return cls(
            market=market,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            exchange=_required_str(value, "exchange").upper(),
            order_division=str(value.get("order_division") or "00").strip() or "00",
            sell_type=str(value.get("sell_type") or "").strip(),
            condition_price=str(value.get("condition_price") or "").strip(),
        )

    @property
    def kafka_key_symbol(self) -> str:
        return self.symbol

    def to_payload(self) -> dict[str, str]:
        payload = {
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "qty": decimal_to_string(self.qty),
            "price": decimal_to_string(self.price),
            "exchange": self.exchange,
            "order_division": self.order_division,
        }
        if self.market == "domestic":
            payload["sell_type"] = self.sell_type
            payload["condition_price"] = self.condition_price
        return payload


@dataclass(frozen=True)
class OrderView:
    order_id: str
    request_id: str
    client_order_id: str
    account_alias: str
    env: str
    market: str
    symbol: str
    side: str
    qty: str
    price: str
    exchange: str
    order_division: str
    status: OrderStatus
    created_at: str | None = None
    updated_at: str | None = None

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "client_order_id": self.client_order_id,
            "account_alias": self.account_alias,
            "env": self.env,
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "exchange": self.exchange,
            "order_division": self.order_division,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def kafka_message_key(account_alias: str, symbol: str) -> str:
    return f"{account_alias}:{symbol}"


def decimal_to_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".") or "0"


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _required_str(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise OrderValidationError(f"{field} is required.")
    return item.strip()


def _decimal_field(value: dict[str, Any], field: str) -> Decimal:
    raw_value = value.get(field)
    if isinstance(raw_value, Decimal):
        return raw_value
    if not isinstance(raw_value, (str, int, float)):
        raise OrderValidationError(f"{field} must be numeric.")
    try:
        return Decimal(str(raw_value))
    except InvalidOperation as exc:
        raise OrderValidationError(f"{field} must be numeric.") from exc
