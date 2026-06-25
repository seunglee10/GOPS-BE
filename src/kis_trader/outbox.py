from __future__ import annotations

from dataclasses import dataclass

from .config import KisConfig
from .kafka_producer import produce_json
from .repository import PostgresOrderRepository


@dataclass(frozen=True)
class OutboxPublishSummary:
    scanned: int
    published: int


def publish_pending_outbox(
    *,
    config: KisConfig,
    repository: PostgresOrderRepository | None = None,
    limit: int = 100,
) -> OutboxPublishSummary:
    repo = repository or PostgresOrderRepository(config.database_url)
    events = repo.fetch_pending_outbox(limit=limit)
    published = 0
    for event in events:
        produce_json(
            bootstrap_servers=config.kafka_bootstrap_servers,
            topic=event.topic,
            key=event.message_key,
            value=event.payload,
        )
        repo.mark_outbox_published(event.event_id)
        published += 1
    return OutboxPublishSummary(scanned=len(events), published=published)
