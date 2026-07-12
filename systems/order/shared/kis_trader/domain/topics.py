"""Canonical Kafka topic contract for the order path."""

from __future__ import annotations

from .status import OrderContractError

ORDERS_COMMANDS_TOPIC = "orders.commands.v1"
SUBMIT_RESULTS_TOPIC = "broker.submit-results.v1"
ORDER_EVENTS_TOPIC = "broker.order-events.v1"
ORDERS_FILLS_TOPIC = "orders.fills.v1"
ORDERS_DLQ_TOPIC = "orders.dlq.v1"

CANONICAL_ORDER_TOPICS: tuple[str, ...] = (
    ORDERS_COMMANDS_TOPIC,
    SUBMIT_RESULTS_TOPIC,
    ORDER_EVENTS_TOPIC,
    ORDERS_FILLS_TOPIC,
    ORDERS_DLQ_TOPIC,
)


def assert_canonical_topic(topic: str) -> None:
    if topic not in CANONICAL_ORDER_TOPICS:
        raise OrderContractError(f"unsupported order topic: {topic}")


def build_order_message_key(account_alias: str, symbol: str) -> str:
    account = _require_non_empty(account_alias, "account_alias")
    normalized_symbol = _require_non_empty(symbol, "symbol").upper()
    return f"{account}:{normalized_symbol}"


def _require_non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderContractError(f"{field} must be a non-empty string")
    return value.strip()
