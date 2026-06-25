from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .client import KisApiError, KisOverseasClient
from .config import KisConfig
from .models import DomesticOrderRequest, OverseasOrderRequest
from .order_contract import OrderCommandEnvelope, OrderContractError, OrderStatus, validate_order_command_message
from .redaction import redact_sensitive
from .repository import PostgresOrderRepository, SubmissionRecord


class RetryableBrokerError(RuntimeError):
    """Raised when a command should be retried without committing its Kafka offset."""


class OrderRepository(Protocol):
    def has_submission(self, request_id: str) -> bool: ...

    def begin_submission(self, envelope: OrderCommandEnvelope) -> str: ...

    def record_submission_result(
        self,
        *,
        envelope: OrderCommandEnvelope,
        order_id: str,
        record: SubmissionRecord,
        result_topic: str,
    ) -> None: ...

    def record_dlq(
        self,
        *,
        topic: str,
        key: str,
        payload: dict[str, Any],
        original_topic: str,
        partition: int | None,
        offset: int | None,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> None: ...


@dataclass(frozen=True)
class BrokerProcessResult:
    handled: bool
    request_id: str | None
    status: str
    skipped_external_submit: bool = False


class KisBrokerAdapter:
    def __init__(
        self,
        *,
        config: KisConfig,
        client: KisOverseasClient,
        repository: OrderRepository | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.repository = repository or PostgresOrderRepository(config.database_url)

    def process_message(
        self,
        value: dict[str, Any],
        *,
        key: str | None,
        original_topic: str,
        partition: int | None = None,
        offset: int | None = None,
    ) -> BrokerProcessResult:
        try:
            envelope = validate_order_command_message(value, key=key)
        except OrderContractError as exc:
            self.repository.record_dlq(
                topic=self.config.kafka_dlq_topic,
                key=key or "",
                payload=value if isinstance(value, dict) else {"raw": repr(value)},
                original_topic=original_topic,
                partition=partition,
                offset=offset,
                error_type=type(exc).__name__,
                error_message=str(exc),
                retryable=False,
            )
            return BrokerProcessResult(
                handled=True,
                request_id=None,
                status="DLQ",
                skipped_external_submit=True,
            )

        if self.repository.has_submission(envelope.request_id):
            return BrokerProcessResult(
                handled=True,
                request_id=envelope.request_id,
                status="DUPLICATE_SUBMISSION",
                skipped_external_submit=True,
            )

        order_id = self.repository.begin_submission(envelope)
        record = self._submit_to_kis(envelope)
        self.repository.record_submission_result(
            envelope=envelope,
            order_id=order_id,
            record=record,
            result_topic=self.config.kafka_submit_results_topic,
        )
        return BrokerProcessResult(
            handled=True,
            request_id=envelope.request_id,
            status=record.status.value,
            skipped_external_submit=False,
        )

    def _submit_to_kis(self, envelope: OrderCommandEnvelope) -> SubmissionRecord:
        request_preview = self._preview_request(envelope)
        try:
            response = self._submit_once(envelope)
        except KisApiError as exc:
            if self._is_token_error(exc):
                self.client.auth.invalidate_access_token()
                try:
                    response = self._submit_once(envelope)
                except KisApiError as retry_exc:
                    return self._record_from_kis_error(request_preview, retry_exc)
            else:
                return self._record_from_kis_error(request_preview, exc)
        return SubmissionRecord(
            status=OrderStatus.SUBMITTED,
            redacted_request=redact_sensitive(request_preview),
            redacted_response=redact_sensitive(response),
            http_status=200,
            kis_msg_cd=_find_first(response, ["msg_cd", "MSG_CD"]),
            kis_order_id=extract_kis_order_id(response),
        )

    def _submit_once(self, envelope: OrderCommandEnvelope) -> dict[str, Any]:
        command = envelope.command
        if command.market == "overseas":
            request = OverseasOrderRequest.from_strings(
                symbol=command.symbol,
                side=command.side,
                qty=format(command.qty, "f"),
                price=format(command.price, "f"),
                exchange=command.exchange,
                order_division=command.order_division,
            )
            return self.client.order(request)
        if command.market == "domestic":
            request = DomesticOrderRequest.from_strings(
                symbol=command.symbol,
                side=command.side,
                qty=format(command.qty, "f"),
                price=format(command.price, "f"),
                exchange=command.exchange,
                order_division=command.order_division,
                sell_type=command.sell_type,
                condition_price=command.condition_price,
            )
            return self.client.domestic_order(request)
        raise OrderContractError(f"Unsupported market: {command.market}")

    def _preview_request(self, envelope: OrderCommandEnvelope) -> dict[str, Any]:
        command = envelope.command
        if command.market == "overseas":
            request = OverseasOrderRequest.from_strings(
                symbol=command.symbol,
                side=command.side,
                qty=format(command.qty, "f"),
                price=format(command.price, "f"),
                exchange=command.exchange,
                order_division=command.order_division,
            )
            return self.client.preview_order(request)
        request = DomesticOrderRequest.from_strings(
            symbol=command.symbol,
            side=command.side,
            qty=format(command.qty, "f"),
            price=format(command.price, "f"),
            exchange=command.exchange,
            order_division=command.order_division,
            sell_type=command.sell_type,
            condition_price=command.condition_price,
        )
        return self.client.preview_domestic_order(request)

    def _record_from_kis_error(self, request_preview: dict[str, Any], exc: KisApiError) -> SubmissionRecord:
        if self._is_retryable_http_error(exc):
            raise RetryableBrokerError(str(exc)) from exc

        cause = exc.__cause__
        if isinstance(cause, (requests.Timeout, requests.ConnectionError)):
            status = OrderStatus.SUBMIT_FAILED_UNKNOWN
            error_type = type(cause).__name__
        elif exc.response:
            status = OrderStatus.REJECTED
            error_type = "KisRejected"
        else:
            status = OrderStatus.SUBMIT_FAILED_UNKNOWN
            error_type = "KisUnknownFailure"

        return SubmissionRecord(
            status=status,
            redacted_request=redact_sensitive(request_preview),
            redacted_response=redact_sensitive(exc.response or {"message": str(exc)}),
            http_status=exc.status_code,
            kis_msg_cd=_find_first(exc.response, ["msg_cd", "MSG_CD"]) if exc.response else None,
            kis_order_id=extract_kis_order_id(exc.response),
            error_type=error_type,
        )

    @staticmethod
    def _is_token_error(exc: KisApiError) -> bool:
        return exc.status_code in {401, 403}

    @staticmethod
    def _is_retryable_http_error(exc: KisApiError) -> bool:
        return exc.status_code == 429 or (exc.status_code is not None and exc.status_code >= 500)


def extract_kis_order_id(payload: Any) -> str | None:
    return _find_first(
        payload,
        [
            "ODNO",
            "odno",
            "ORD_NO",
            "ord_no",
            "KIS_ORDER_ID",
            "kis_order_id",
            "order_no",
        ],
    )


def _find_first(payload: Any, keys: list[str]) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for value in payload.values():
            found = _find_first(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_first(value, keys)
            if found:
                return found
    return None
