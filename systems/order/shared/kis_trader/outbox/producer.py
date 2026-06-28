"""Kafka producer wrappers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from confluent_kafka import KafkaException, Producer as ConfluentProducer


class Producer(Protocol):
    def produce(self, topic: str, key: str, value: dict[str, Any]) -> None:
        ...


class KafkaPublishError(RuntimeError):
    """Raised when a Kafka outbox event cannot be delivered."""


@dataclass
class RecordingProducer:
    messages: list[dict[str, Any]] = field(default_factory=list)

    def produce(self, topic: str, key: str, value: dict[str, Any]) -> None:
        self.messages.append({"topic": topic, "key": key, "value": value})


class KafkaJsonProducer:
    def __init__(self, bootstrap_servers: str, **overrides: Any) -> None:
        config = {
            "bootstrap.servers": bootstrap_servers,
            "acks": "all",
            "enable.idempotence": True,
            "message.timeout.ms": 10000,
        }
        config.update(overrides)
        self.config = config
        self._producer = ConfluentProducer(config)

    @classmethod
    def from_env(cls) -> "KafkaJsonProducer":
        return cls(os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092"))

    def produce(self, topic: str, key: str, value: dict[str, Any]) -> None:
        errors: list[KafkaException] = []

        def on_delivery(error: KafkaException | None, _message: Any) -> None:
            if error is not None:
                errors.append(error)

        payload = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._producer.produce(topic, key=key.encode("utf-8"), value=payload, callback=on_delivery)
        self._producer.flush()
        if errors:
            raise KafkaPublishError(str(errors[0]))
