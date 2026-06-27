"""KIS Broker Adapter core workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kis_trader.domain.envelope import validate_order_envelope
from kis_trader.domain.status import OrderContractError, OrderStatus
from kis_trader.kis.fake import KisConnectionReset, KisExplicitReject, KisHttpError, KisTimeout, KisTokenExpired
from kis_trader.kis.payload import build_kis_order_payload
from kis_trader.operations.guardrails import TradingGuardrails
from kis_trader.persistence.repository import OrderRepository
from kis_trader.security.validation import assert_no_forbidden_fields


@dataclass(frozen=True)
class BrokerProcessResult:
    order_id: str | None
    status: str
    skipped_external_submit: bool = False
    sent_to_dlq: bool = False
    reason: str | None = None


class KisBrokerAdapter:
    def __init__(
        self,
        repository: OrderRepository,
        kis_client: Any,
        *,
        guardrails: TradingGuardrails | None = None,
    ) -> None:
        self.repository = repository
        self.kis_client = kis_client
        self.guardrails = guardrails or TradingGuardrails()

    def process_message(self, message: dict[str, Any]) -> BrokerProcessResult:
        try:
            assert_no_forbidden_fields(message)
            command = validate_order_envelope(message)
        except Exception as exc:
            self.repository.record_dlq(source="orders.commands.v1", message=message, error=exc)
            return BrokerProcessResult(order_id=None, status="DLQ", sent_to_dlq=True, reason=str(exc))

        ensure = getattr(self.repository, "ensure_published_order_for_command", None)
        if callable(ensure):
            ensure(command)

        existing = self.repository.find_submission(command.request_id, command.client_order_id)
        if existing is not None:
            existing_result = self._handle_existing_submission(command, existing)
            if existing_result is not None:
                return existing_result
            submission = existing
        else:
            intent = self.repository.claim_submission_intent(command)
            if not intent.created:
                existing_result = self._handle_existing_submission(command, intent.submission)
                if existing_result is not None:
                    return existing_result
            submission = intent.submission

        try:
            self.repository.update_order_status(command.order_id, OrderStatus.SUBMITTING)
        except OrderContractError as exc:
            self.repository.record_dlq(source="orders.commands.v1", message=message, error=exc)
            return BrokerProcessResult(order_id=command.order_id, status="DLQ", sent_to_dlq=True, reason=str(exc))

        decision = self.guardrails.check(command)
        if not decision.allowed:
            self.repository.record_submission_result(
                submission,
                OrderStatus.RISK_REJECTED,
                response={"reason": decision.reason},
                reason=decision.reason,
            )
            self.repository.record_audit("policy_rejected", command.order_id, decision.reason)
            return BrokerProcessResult(order_id=command.order_id, status=OrderStatus.RISK_REJECTED.value, reason=decision.reason)

        build_kis_order_payload(command)
        status, response, reason = self._submit_with_policy(command)
        self._record_guardrail_observation(command.account_alias, status)
        self.repository.record_submission_result(
            submission,
            status,
            response=response,
            reason=reason,
            broker_order_id=response.get("broker_order_id") if response else None,
        )
        return BrokerProcessResult(order_id=command.order_id, status=status.value, reason=reason)

    def _handle_existing_submission(self, command: Any, submission: dict[str, Any]) -> BrokerProcessResult | None:
        if submission.get("status") != "INTENT_RECORDED":
            return BrokerProcessResult(
                order_id=submission["order_id"],
                status=str(submission["status"]),
                skipped_external_submit=True,
                reason="durable submission result already exists",
            )

        order = self.repository.get_order(command.order_id)
        if order is not None and order["status"] == OrderStatus.PUBLISHED.value:
            # Intent was claimed but KIS POST had not started; continue safely.
            return None
        if order is not None and order["status"] == OrderStatus.SUBMITTING.value:
            reason = "submission intent exists without a durable KIS result"
            self.repository.record_submission_result(
                submission,
                OrderStatus.SUBMIT_FAILED_UNKNOWN,
                response={"reason": reason},
                reason=reason,
            )
            return BrokerProcessResult(
                order_id=submission["order_id"],
                status=OrderStatus.SUBMIT_FAILED_UNKNOWN.value,
                skipped_external_submit=True,
                reason=reason,
            )

        return BrokerProcessResult(
            order_id=submission["order_id"],
            status=str(submission["status"]),
            skipped_external_submit=True,
            reason="durable submission intent already exists",
        )

    def _submit_with_policy(self, command: Any) -> tuple[OrderStatus, dict[str, Any], str | None]:
        try:
            response = self.kis_client.submit_order(command)
            return self._classify_success_response(response)
        except KisTokenExpired:
            self.kis_client.refresh_token()
            try:
                response = self.kis_client.submit_order(command)
                return self._classify_success_response(response)
            except KisExplicitReject as exc:
                return OrderStatus.REJECTED, {"error": str(exc)}, str(exc)
            except (KisTimeout, KisConnectionReset, KisHttpError) as exc:
                return self._classify_uncertain_error(exc)
        except KisExplicitReject as exc:
            return OrderStatus.REJECTED, {"error": str(exc)}, str(exc)
        except (KisTimeout, KisConnectionReset, KisHttpError) as exc:
            if isinstance(exc, KisHttpError) and exc.safe_to_retry:
                return self._retry_once_after_safe_http_error(command, exc)
            return self._classify_uncertain_error(exc)

    def _classify_success_response(self, response: dict[str, Any]) -> tuple[OrderStatus, dict[str, Any], str | None]:
        if response.get("rt_cd") == "0" or response.get("accepted") is True:
            return OrderStatus.SUBMITTED, response, None
        return OrderStatus.REJECTED, response, "KIS explicit reject"

    def _retry_once_after_safe_http_error(self, command: Any, original_error: KisHttpError) -> tuple[OrderStatus, dict[str, Any], str | None]:
        try:
            response = self.kis_client.submit_order(command)
            return self._classify_success_response(response)
        except KisExplicitReject as exc:
            return OrderStatus.REJECTED, {"error": str(exc), "after": str(original_error)}, str(exc)
        except (KisTimeout, KisConnectionReset, KisHttpError) as exc:
            return self._classify_uncertain_error(exc)

    def _classify_uncertain_error(self, exc: Exception) -> tuple[OrderStatus, dict[str, Any], str | None]:
        return OrderStatus.SUBMIT_FAILED_UNKNOWN, {"error": str(exc)}, str(exc)

    def _record_guardrail_observation(self, account_alias: str, status: OrderStatus) -> None:
        if status == OrderStatus.SUBMIT_FAILED_UNKNOWN:
            self.guardrails.record_broker_outcome(account_alias, "timeout")
        if status == OrderStatus.REJECTED:
            self.guardrails.record_broker_outcome(account_alias, "reject")
