"""Repository contracts for the order reliability workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from kis_trader.domain.commands import OrderCommand
from kis_trader.domain.status import OrderStatus


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RepositoryError(RuntimeError):
    """Base persistence error."""


class IdempotencyConflictError(RepositoryError):
    """Raised when one idempotency key is reused with a different body."""


class OrderNotFoundError(RepositoryError):
    """Raised when an operation references an unknown order."""


@dataclass(frozen=True)
class OrderCreationResult:
    created: bool
    idempotent_replay: bool
    order: dict[str, Any]
    response: dict[str, Any]
    outbox_event_id: str | None = None


@dataclass(frozen=True)
class SubmissionIntent:
    created: bool
    submission: dict[str, Any]


class OrderRepository(Protocol):
    def create_received_order(
        self,
        *,
        idempotency_key_hash: str,
        body_hash: str,
        command: OrderCommand,
        user_sub: str | None = None,
    ) -> OrderCreationResult:
        ...

    def find_idempotent_response(self, idempotency_key_hash: str, body_hash: str) -> dict[str, Any] | None:
        """이미 같은 키+본문으로 접수된 요청의 저장된 응답 (replay 프로브).

        리스크 체크보다 먼저 호출해, 멱등 재시도가 리스크 판정에 다시 걸리지
        않게 한다. 키는 같은데 본문이 다르면 None (충돌은 create에서 409).
        """
        ...

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        ...

    def list_order_events(self, order_id: str) -> list[dict[str, Any]]:
        ...

    def update_order_status(self, order_id: str, status: OrderStatus, reason: str | None = None) -> str:
        ...

    def fetch_pending_outbox(self, limit: int | None = None, topic: str | None = None) -> list[dict[str, Any]]:
        ...

    def claim_pending_outbox(
        self,
        *,
        worker_id: str,
        limit: int | None = None,
        topic: str | None = None,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        ...

    def mark_outbox_published(self, event_id: str, *, worker_id: str | None = None) -> None:
        ...

    def mark_outbox_failed(
        self,
        event_id: str,
        error: Exception | str,
        *,
        worker_id: str | None = None,
        retry_delay_seconds: int = 5,
    ) -> None:
        ...

    def inbox_event_seen(self, consumer_name: str, event_id: str) -> bool:
        ...

    def record_inbox_event(
        self,
        consumer_name: str,
        event_id: str,
        *,
        payload_digest: str | None = None,
    ) -> bool:
        ...

    def claim_submission_intent(self, command: OrderCommand) -> SubmissionIntent:
        ...

    def find_submission(self, request_id: str, client_order_id: str) -> dict[str, Any] | None:
        ...

    def record_submission_result(
        self,
        submission: dict[str, Any],
        status: OrderStatus,
        *,
        response: dict[str, Any] | None = None,
        reason: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        ...

    def record_dlq(self, *, source: str, message: Any, error: Exception) -> None:
        ...

    def record_audit(self, action: str, order_id: str, reason: str | None = None) -> None:
        ...

    def find_orders_by_status(self, statuses: set[OrderStatus]) -> list[dict[str, Any]]:
        ...

    def find_open_orders(self) -> list[dict[str, Any]]:
        ...

    def update_reconciled_order(
        self,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
        *,
        execution_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ...

    def metrics_snapshot(self) -> dict[str, Any]:
        ...
