from __future__ import annotations

from dataclasses import dataclass

from .producer import JsonProducer
from .storage import OutboxStorage


@dataclass(frozen=True)
class OutboxPublishSummary:
    scanned: int
    published: int


@dataclass(frozen=True)
class OutboxPublisherService:
    storage: OutboxStorage
    producer: JsonProducer

    def publish_pending(self, *, limit: int = 100) -> OutboxPublishSummary:
        events = self.storage.fetch_pending_outbox(limit=limit)
        published = 0
        for event in events:
            self.producer.produce_json(topic=event.topic, key=event.message_key, value=event.payload)
            self.storage.mark_outbox_published(
                event_id=event.event_id,
                topic=event.topic,
                order_id=event.order_id,
            )
            published += 1
        return OutboxPublishSummary(scanned=len(events), published=published)
