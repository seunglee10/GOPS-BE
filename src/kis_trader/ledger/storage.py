from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kis_trader.contracts.statuses import OrderStatus


@dataclass(frozen=True)
class SubmissionInput:
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
    status: OrderStatus
    raw_request: dict[str, Any]
    raw_response: dict[str, Any]
    http_status: int | None = None
    kis_msg_cd: str | None = None
    kis_order_id: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class SubmissionRecord:
    order_id: str
    request_id: str
    client_order_id: str
    account_alias: str
    status: OrderStatus
    event_type: str
    event_payload: dict[str, Any]
    redacted_request: dict[str, Any]
    redacted_response: dict[str, Any]
    http_status: int | None
    kis_msg_cd: str | None
    kis_order_id: str | None
    error_type: str | None
    result_topic: str
    result_event_id: str
    result_payload: dict[str, Any]
    message_key: str


@dataclass(frozen=True)
class SubmissionRecordResult:
    submission_id: str
    created: bool


class SubmissionStorage(Protocol):
    def record_submission_result(self, record: SubmissionRecord) -> SubmissionRecordResult: ...
