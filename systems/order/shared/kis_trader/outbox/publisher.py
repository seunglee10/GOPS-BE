"""Publish pending DB outbox rows to Kafka."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from kis_trader.persistence.repository import OrderRepository

from .producer import Producer


def publish_pending_outbox(repository: OrderRepository, producer: Producer, *, limit: int | None = None, topic: str | None = None) -> int:
    published = 0
    worker_id = f"outbox-publisher-{uuid4().hex}"
    for event in repository.claim_pending_outbox(worker_id=worker_id, limit=limit, topic=topic):
        key = event.get("message_key") or event.get("key") or event["order_id"]
        try:
            producer.produce(event["topic"], key=str(key), value=dict(event["payload"]))
        except Exception as exc:
            repository.mark_outbox_failed(event["event_id"], exc, worker_id=worker_id)
            raise
        repository.mark_outbox_published(event["event_id"], worker_id=worker_id)
        published += 1
    return published
