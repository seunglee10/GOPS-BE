from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from kis_trader.config import AppSettings
from kis_trader.contracts.envelope import build_submit_result_envelope
from kis_trader.contracts.order import kafka_message_key
from kis_trader.contracts.redaction import redact_sensitive
from kis_trader.contracts.statuses import OrderStatus, SUBMISSION_RESULT_STATUSES

from .storage import SubmissionInput, SubmissionRecord, SubmissionRecordResult, SubmissionStorage


class SubmissionStatusError(ValueError):
    """Raised when a non-submit-result status is written through M4 ledger."""


@dataclass(frozen=True)
class SubmissionLedgerService:
    settings: AppSettings
    storage: SubmissionStorage

    def record_submission_result(self, item: SubmissionInput) -> SubmissionRecordResult:
        if item.status not in SUBMISSION_RESULT_STATUSES:
            raise SubmissionStatusError(f"{item.status.value} is not a submit-result status.")

        event_id = str(uuid4())
        result_payload = build_submit_result_envelope(
            env=item.env,
            account_alias=item.account_alias,
            order_id=item.order_id,
            request_id=item.request_id,
            client_order_id=item.client_order_id,
            status=item.status,
            market=item.market,
            symbol=item.symbol,
            side=item.side,
            qty=item.qty,
            price=item.price,
            exchange=item.exchange,
            kis_order_id=item.kis_order_id,
            kis_msg_cd=item.kis_msg_cd,
            error_type=item.error_type,
            event_id=event_id,
        )
        record = SubmissionRecord(
            order_id=item.order_id,
            request_id=item.request_id,
            client_order_id=item.client_order_id,
            account_alias=item.account_alias,
            status=item.status,
            event_type=_event_type_for_status(item.status),
            event_payload=result_payload,
            redacted_request=redact_sensitive(item.raw_request),
            redacted_response=redact_sensitive(item.raw_response),
            http_status=item.http_status,
            kis_msg_cd=item.kis_msg_cd,
            kis_order_id=item.kis_order_id,
            error_type=item.error_type,
            result_topic=self.settings.kafka_submit_results_topic,
            result_event_id=event_id,
            result_payload=result_payload,
            message_key=kafka_message_key(item.account_alias, item.symbol),
        )
        return self.storage.record_submission_result(record)


def _event_type_for_status(status: OrderStatus) -> str:
    return {
        OrderStatus.SUBMITTED: "order.submitted",
        OrderStatus.REJECTED: "order.rejected",
        OrderStatus.RISK_REJECTED: "order.risk_rejected",
        OrderStatus.SUBMIT_FAILED_UNKNOWN: "order.submit_failed_unknown",
        OrderStatus.FAILED: "order.failed",
    }[status]
