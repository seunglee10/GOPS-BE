from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class KafkaPublishError(RuntimeError):
    """Raised when Kafka does not acknowledge an outbox event."""


@dataclass(frozen=True)
class ProduceResult:
    partition: int | None
    offset: int | None


class JsonProducer(Protocol):
    def produce_json(self, *, topic: str, key: str, value: dict[str, Any]) -> ProduceResult: ...


@dataclass(frozen=True)
class KafkaJsonProducer:
    bootstrap_servers: str
    message_timeout_ms: int = 10000

    def produce_json(self, *, topic: str, key: str, value: dict[str, Any]) -> ProduceResult:
        try:
            from confluent_kafka import KafkaException, Producer
        except ImportError as exc:
            raise KafkaPublishError("confluent-kafka is not installed. Run uv sync first.") from exc

        producer = Producer(
            {
                "bootstrap.servers": self.bootstrap_servers,
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": self.message_timeout_ms,
            }
        )
        delivered: dict[str, int | None] = {"partition": None, "offset": None}
        delivery_error: KafkaPublishError | None = None
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

        def on_delivery(error: Any, message: Any) -> None:
            nonlocal delivery_error
            if error is not None:
                delivery_error = KafkaPublishError(f"Kafka delivery failed: {error}")
                return
            delivered["partition"] = message.partition()
            delivered["offset"] = message.offset()

        try:
            producer.produce(topic=topic, key=key.encode("utf-8"), value=encoded, on_delivery=on_delivery)
            remaining = producer.flush(self.message_timeout_ms / 1000)
        except KafkaException as exc:
            raise KafkaPublishError(f"Kafka produce failed: {exc}") from exc

        if remaining:
            raise KafkaPublishError(f"Kafka delivery timed out with {remaining} message(s) still queued.")
        if delivery_error is not None:
            raise delivery_error
        return ProduceResult(partition=delivered["partition"], offset=delivered["offset"])
