"""Canonical order status model from docs/spec.md."""

from __future__ import annotations

from enum import StrEnum


class OrderContractError(ValueError):
    """Raised when an order contract is violated."""


class OrderStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMIT_FAILED_UNKNOWN = "SUBMIT_FAILED_UNKNOWN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    FAILED = "FAILED"


CANONICAL_STATUSES: tuple[str, ...] = tuple(status.value for status in OrderStatus)

ALLOWED_TRANSITIONS: frozenset[tuple[OrderStatus, OrderStatus]] = frozenset(
    {
        (OrderStatus.RECEIVED, OrderStatus.PUBLISHED),
        (OrderStatus.RECEIVED, OrderStatus.REJECTED),
        (OrderStatus.PUBLISHED, OrderStatus.SUBMITTING),
        (OrderStatus.SUBMITTING, OrderStatus.SUBMITTED),
        (OrderStatus.SUBMITTING, OrderStatus.REJECTED),
        (OrderStatus.SUBMITTING, OrderStatus.RISK_REJECTED),
        (OrderStatus.SUBMITTING, OrderStatus.SUBMIT_FAILED_UNKNOWN),
        (OrderStatus.SUBMITTING, OrderStatus.FAILED),
        (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.SUBMITTED, OrderStatus.FILLED),
        (OrderStatus.SUBMITTED, OrderStatus.CANCELED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.SUBMITTED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.FILLED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.REJECTED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.RECONCILIATION_REQUIRED),
        (OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.SUBMITTED),
        (OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.FILLED),
        (OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.CANCELED),
    }
)

TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.REJECTED,
        OrderStatus.RISK_REJECTED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.FAILED,
    }
)

RECONCILIATION_TARGET_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.SUBMIT_FAILED_UNKNOWN,
        OrderStatus.RECONCILIATION_REQUIRED,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
    }
)

USER_DISPLAY_STATUS: dict[OrderStatus, str] = {
    OrderStatus.RECEIVED: "주문 접수됨",
    OrderStatus.PUBLISHED: "처리 중",
    OrderStatus.SUBMITTING: "제출 중",
    OrderStatus.SUBMITTED: "주문 제출됨",
    OrderStatus.SUBMIT_FAILED_UNKNOWN: "주문 확인 중",
    OrderStatus.RECONCILIATION_REQUIRED: "주문 확인 중",
    OrderStatus.PARTIALLY_FILLED: "일부 체결",
    OrderStatus.FILLED: "체결 완료",
    OrderStatus.CANCELED: "취소됨",
    OrderStatus.REJECTED: "주문 거부됨",
    OrderStatus.RISK_REJECTED: "주문 거부됨",
    OrderStatus.FAILED: "실패",
}


def coerce_status(value: OrderStatus | str) -> OrderStatus:
    try:
        return value if isinstance(value, OrderStatus) else OrderStatus(str(value))
    except ValueError as exc:
        raise OrderContractError(f"unknown order status: {value}") from exc


def assert_transition_allowed(current: OrderStatus | str, new: OrderStatus | str) -> None:
    current_status = coerce_status(current)
    new_status = coerce_status(new)
    if current_status == new_status:
        return
    if (current_status, new_status) not in ALLOWED_TRANSITIONS:
        raise OrderContractError(f"illegal order transition: {current_status.value} -> {new_status.value}")


def is_terminal_status(status: OrderStatus | str) -> bool:
    return coerce_status(status) in TERMINAL_STATUSES
