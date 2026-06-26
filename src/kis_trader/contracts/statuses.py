from __future__ import annotations

from enum import StrEnum


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


CANONICAL_ORDER_STATUSES = frozenset(status.value for status in OrderStatus)

SUBMISSION_RESULT_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,
        OrderStatus.RISK_REJECTED,
        OrderStatus.SUBMIT_FAILED_UNKNOWN,
        OrderStatus.FAILED,
    }
)
