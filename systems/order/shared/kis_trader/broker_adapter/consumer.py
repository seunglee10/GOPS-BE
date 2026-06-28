"""Kafka consumer runner for the KIS Broker Adapter."""

from __future__ import annotations

import json
import os
from typing import Any

from confluent_kafka import Consumer as ConfluentConsumer, KafkaException

from kis_trader.domain.topics import ORDERS_COMMANDS_TOPIC
from kis_trader.kis.client import DemoKisHttpClient
from kis_trader.kis.fake import FakeKisClient
from kis_trader.persistence.postgres import PostgresOrderRepository

from .adapter import BrokerProcessResult, KisBrokerAdapter


def build_broker_adapter_consumer_config(bootstrap_servers: str, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "bootstrap.servers": bootstrap_servers,
        "group.id": "kis-broker-adapter",
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    }
    config.update(overrides)
    config["group.id"] = "kis-broker-adapter"
    config["enable.auto.commit"] = False
    return config


class KafkaBrokerAdapterConsumer:
    def __init__(self, consumer: Any, adapter: KisBrokerAdapter, topics: list[str] | None = None) -> None:
        self.consumer = consumer
        self.adapter = adapter
        self.topics = topics or [ORDERS_COMMANDS_TOPIC]
        self.consumer.subscribe(self.topics)

    @classmethod
    def from_env(cls, *, fake_outcome: str | None = None) -> "KafkaBrokerAdapterConsumer":
        if os.getenv("KIS_ENV", "demo").strip().lower() != "demo":
            raise RuntimeError("Only KIS demo trading is implemented. KIS_ENV=real is not allowed.")
        bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092")
        consumer = ConfluentConsumer(build_broker_adapter_consumer_config(bootstrap_servers))
        repository = PostgresOrderRepository.from_env()
        kis_client = FakeKisClient([fake_outcome]) if fake_outcome else DemoKisHttpClient.from_env()
        return cls(consumer, KisBrokerAdapter(repository, kis_client))

    def consume_once(self, timeout_seconds: float = 1.0) -> BrokerProcessResult | None:
        message = self.consumer.poll(timeout_seconds)
        if message is None:
            return None
        if message.error():
            raise KafkaException(message.error())
        payload = json.loads(message.value().decode("utf-8"))
        result = self.adapter.process_message(payload)
        self.consumer.commit(message=message, asynchronous=False)
        return result

    def run(self, *, max_messages: int | None = None, timeout_seconds: float = 1.0) -> int:
        processed = 0
        try:
            while max_messages is None or processed < max_messages:
                result = self.consume_once(timeout_seconds=timeout_seconds)
                if result is None:
                    if max_messages is not None:
                        break
                    continue
                processed += 1
        finally:
            self.consumer.close()
        return processed
