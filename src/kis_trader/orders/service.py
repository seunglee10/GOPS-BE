from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from kis_trader.config import AppSettings
from kis_trader.contracts.envelope import build_order_command_envelope
from kis_trader.contracts.order import OrderCommand, kafka_message_key

from .hashing import hash_idempotency_key, hash_request_body
from .storage import (
    IdempotencyMismatchError,
    OrderAcceptanceDraft,
    OrderAcceptanceResult,
    OrderStorage,
)


class IdempotencyConflict(RuntimeError):
    """Raised when an idempotency key is reused with a different request body."""


@dataclass(frozen=True)
class OrderAcceptanceService:
    settings: AppSettings
    storage: OrderStorage

    def accept(self, *, idempotency_key: str, body: dict[str, Any]) -> OrderAcceptanceResult:
        command = OrderCommand.from_mapping(body)
        canonical_body = command.to_payload()
        order_id = str(uuid4())
        request_id = str(uuid4())
        client_order_id = str(uuid4())
        event_id = str(uuid4())
        command_payload = build_order_command_envelope(
            command=command,
            env=self.settings.env,
            account_alias=self.settings.kafka_account_alias,
            order_id=order_id,
            request_id=request_id,
            client_order_id=client_order_id,
            event_id=event_id,
        )
        draft = OrderAcceptanceDraft(
            command=command,
            env=self.settings.env,
            account_alias=self.settings.kafka_account_alias,
            order_id=order_id,
            request_id=request_id,
            client_order_id=client_order_id,
            idempotency_key_hash=hash_idempotency_key(
                raw_key=idempotency_key,
                secret=self.settings.idempotency_hash_secret,
            ),
            request_body_hash=hash_request_body(canonical_body),
            command_event_id=event_id,
            command_payload=command_payload,
            command_topic=self.settings.kafka_order_commands_topic,
            command_message_key=kafka_message_key(self.settings.kafka_account_alias, command.kafka_key_symbol),
        )
        try:
            return self.storage.accept_order(draft)
        except IdempotencyMismatchError as exc:
            raise IdempotencyConflict("Idempotency-Key was already used with a different body.") from exc

    def get_order(self, order_id: str):
        return self.storage.get_order(order_id)
