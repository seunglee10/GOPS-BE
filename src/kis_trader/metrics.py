from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import KisConfig


@dataclass(frozen=True)
class OperationalMetrics:
    order_status_counts: dict[str, int]
    outbox_unpublished_count: int
    dlq_unpublished_count: int
    reconciliation_mismatch_count: int
    kafka_consumer_lag: dict[str, int] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_status_counts": self.order_status_counts,
            "outbox_unpublished_count": self.outbox_unpublished_count,
            "dlq_unpublished_count": self.dlq_unpublished_count,
            "reconciliation_mismatch_count": self.reconciliation_mismatch_count,
            "kafka_consumer_lag": self.kafka_consumer_lag,
        }


def collect_operational_metrics(config: KisConfig, *, include_kafka: bool = True) -> OperationalMetrics:
    db_metrics = _collect_db_metrics(config.database_url, dlq_topic=config.kafka_dlq_topic)
    kafka_lag = collect_kafka_consumer_lag(config) if include_kafka else None
    return OperationalMetrics(
        order_status_counts=db_metrics["order_status_counts"],
        outbox_unpublished_count=db_metrics["outbox_unpublished_count"],
        dlq_unpublished_count=db_metrics["dlq_unpublished_count"],
        reconciliation_mismatch_count=db_metrics["reconciliation_mismatch_count"],
        kafka_consumer_lag=kafka_lag,
    )


def collect_kafka_consumer_lag(config: KisConfig) -> dict[str, int]:
    try:
        from confluent_kafka import Consumer, TopicPartition
    except ImportError as exc:
        raise RuntimeError("confluent-kafka is not installed. Run uv sync first.") from exc

    topic = config.kafka_order_commands_topic
    consumer = Consumer(
        {
            "bootstrap.servers": config.kafka_bootstrap_servers,
            "group.id": config.kafka_broker_adapter_group_id,
            "enable.auto.commit": False,
        }
    )
    try:
        metadata = consumer.list_topics(topic=topic, timeout=5)
        topic_metadata = metadata.topics.get(topic)
        if topic_metadata is None or topic_metadata.error is not None:
            return {}
        partitions = [TopicPartition(topic, partition_id) for partition_id in topic_metadata.partitions]
        committed = consumer.committed(partitions, timeout=5)
        lags: dict[str, int] = {}
        for partition in committed:
            low, high = consumer.get_watermark_offsets(
                TopicPartition(topic, partition.partition),
                timeout=5,
            )
            committed_offset = partition.offset
            if committed_offset < 0:
                committed_offset = low
            lags[f"{topic}:{partition.partition}"] = max(0, high - committed_offset)
        return lags
    finally:
        consumer.close()


def _collect_db_metrics(database_url: str, *, dlq_topic: str) -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg is not installed. Run uv sync first.") from exc

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        status_rows = conn.execute("SELECT status, count(*) AS count FROM orders GROUP BY status").fetchall()
        outbox_row = conn.execute(
            "SELECT count(*) AS count FROM outbox_events WHERE published_at IS NULL"
        ).fetchone()
        dlq_row = conn.execute(
            "SELECT count(*) AS count FROM outbox_events WHERE published_at IS NULL AND topic = %s",
            (dlq_topic,),
        ).fetchone()
        mismatch_row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE((result->>'mismatches')::integer, 0)), 0) AS count
            FROM reconciliation_runs
            """
        ).fetchone()
    return {
        "order_status_counts": {str(row["status"]): int(row["count"]) for row in status_rows},
        "outbox_unpublished_count": int(outbox_row["count"]),
        "dlq_unpublished_count": int(dlq_row["count"]),
        "reconciliation_mismatch_count": int(mismatch_row["count"]),
    }
