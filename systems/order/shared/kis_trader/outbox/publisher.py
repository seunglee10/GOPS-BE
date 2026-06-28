"""Publish pending DB outbox rows to Kafka."""

from __future__ import annotations

from typing import Any

from kis_trader.persistence.repository import OrderRepository

from .producer import Producer


def publish_pending_outbox(repository: OrderRepository, producer: Producer, *, limit: int | None = None, topic: str | None = None) -> int:
    published = 0
    for event in repository.fetch_pending_outbox(limit=limit, topic=topic):
        key = event.get("message_key") or event.get("key") or event["order_id"]
        producer.produce(event["topic"], key=str(key), value=dict(event["payload"]))
        repository.mark_outbox_published(event["event_id"])
        published += 1
    return published
