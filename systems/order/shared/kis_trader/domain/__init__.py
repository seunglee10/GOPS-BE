"""Canonical order domain contracts."""

from .commands import OrderCommand, OrderRequest, validate_order_request_payload
from .envelope import (
    KafkaEnvelope,
    build_order_command_envelope,
    build_order_fill_envelope,
    build_order_status_envelope,
    validate_order_envelope,
)
from .status import CANONICAL_STATUSES, OrderStatus, assert_transition_allowed
from .topics import (
    CANONICAL_ORDER_TOPICS,
    ORDER_EVENTS_TOPIC,
    ORDERS_COMMANDS_TOPIC,
    ORDERS_DLQ_TOPIC,
    ORDERS_FILLS_TOPIC,
    SUBMIT_RESULTS_TOPIC,
    build_order_message_key,
)

__all__ = [
    "CANONICAL_ORDER_TOPICS",
    "CANONICAL_STATUSES",
    "KafkaEnvelope",
    "ORDER_EVENTS_TOPIC",
    "ORDERS_COMMANDS_TOPIC",
    "ORDERS_DLQ_TOPIC",
    "ORDERS_FILLS_TOPIC",
    "OrderCommand",
    "OrderRequest",
    "OrderStatus",
    "SUBMIT_RESULTS_TOPIC",
    "assert_transition_allowed",
    "build_order_command_envelope",
    "build_order_fill_envelope",
    "build_order_message_key",
    "build_order_status_envelope",
    "validate_order_envelope",
    "validate_order_request_payload",
]
