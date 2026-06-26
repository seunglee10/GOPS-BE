from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kis_trader.contracts.order import OrderCommand, OrderView


@dataclass(frozen=True)
class OrderAcceptanceDraft:
    command: OrderCommand
    env: str
    account_alias: str
    order_id: str
    request_id: str
    client_order_id: str
    idempotency_key_hash: str
    request_body_hash: str
    command_event_id: str
    command_payload: dict[str, object]
    command_topic: str
    command_message_key: str


@dataclass(frozen=True)
class OrderAcceptanceResult:
    order: OrderView
    created: bool


class IdempotencyMismatchError(RuntimeError):
    """Raised when an idempotency key is reused with a different body."""


class OrderStorage(Protocol):
    def accept_order(self, draft: OrderAcceptanceDraft) -> OrderAcceptanceResult: ...

    def get_order(self, order_id: str) -> OrderView | None: ...
