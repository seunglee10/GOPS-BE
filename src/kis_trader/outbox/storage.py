from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    order_id: str | None
    topic: str
    message_key: str
    payload: dict[str, Any]


class OutboxStorage(Protocol):
    def fetch_pending_outbox(self, *, limit: int) -> list[OutboxEvent]: ...

    def mark_outbox_published(self, *, event_id: str, topic: str, order_id: str | None) -> None: ...
