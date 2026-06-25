from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .broker_adapter import KisBrokerAdapter, RetryableBrokerError
from .client import KisOverseasClient
from .config import KisConfig
from .repository import PostgresOrderRepository


@dataclass(frozen=True)
class AdapterRunSummary:
    processed: int
    dlq_or_skipped: int
    retryable_errors: int


class SimulatedAdapterCrash(RuntimeError):
    """Raised by smoke tests to stop before committing Kafka offsets."""


def run_broker_adapter_consumer(
    *,
    config: KisConfig,
    client: KisOverseasClient,
    max_messages: int | None = None,
    poll_timeout_seconds: float = 1.0,
    crash_before_process: bool = False,
    crash_after_process: bool = False,
) -> AdapterRunSummary:
    try:
        from confluent_kafka import Consumer, KafkaException
    except ImportError as exc:
        raise RuntimeError("confluent-kafka is not installed. Run uv sync first.") from exc

    repository = PostgresOrderRepository(config.database_url)
    repository.ensure_schema()
    adapter = KisBrokerAdapter(config=config, client=client, repository=repository)
    consumer = Consumer(
        {
            "bootstrap.servers": config.kafka_bootstrap_servers,
            "group.id": config.kafka_broker_adapter_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([config.kafka_order_commands_topic])

    processed = 0
    dlq_or_skipped = 0
    retryable_errors = 0
    try:
        while max_messages is None or processed < max_messages:
            message = consumer.poll(poll_timeout_seconds)
            if message is None:
                if max_messages is not None:
                    break
                continue
            if message.error():
                raise KafkaException(message.error())

            key = message.key().decode("utf-8") if message.key() else None
            if crash_before_process:
                raise SimulatedAdapterCrash("simulated crash before processing Kafka message")
            try:
                value = json.loads(message.value().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                repository.record_dlq(
                    topic=config.kafka_dlq_topic,
                    key=key or "",
                    payload={"raw": repr(message.value())},
                    original_topic=message.topic(),
                    partition=message.partition(),
                    offset=message.offset(),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    retryable=False,
                )
                consumer.commit(message)
                processed += 1
                dlq_or_skipped += 1
                continue

            try:
                result = adapter.process_message(
                    value,
                    key=key,
                    original_topic=message.topic(),
                    partition=message.partition(),
                    offset=message.offset(),
                )
            except RetryableBrokerError:
                retryable_errors += 1
                time.sleep(1)
                continue

            if crash_after_process:
                raise SimulatedAdapterCrash("simulated crash after processing before offset commit")
            consumer.commit(message)
            processed += 1
            if result.skipped_external_submit or result.status == "DLQ":
                dlq_or_skipped += 1
    finally:
        consumer.close()
    return AdapterRunSummary(
        processed=processed,
        dlq_or_skipped=dlq_or_skipped,
        retryable_errors=retryable_errors,
    )
