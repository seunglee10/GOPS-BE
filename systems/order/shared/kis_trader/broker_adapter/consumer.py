"""Kafka consumer runner for the KIS Broker Adapter."""

from __future__ import annotations

import json
import hashlib
import os
import time
from typing import Any

from confluent_kafka import Consumer as ConfluentConsumer, KafkaError, KafkaException

from kis_trader.domain.topics import ORDERS_COMMANDS_TOPIC
from kis_trader.kis.client import DemoKisHttpClient
from kis_trader.kis.fake import FakeKisClient
from kis_trader.persistence.postgres import PostgresOrderRepository
from kis_trader.runtime_heartbeat import touch_heartbeat

from .adapter import BrokerProcessResult, KisBrokerAdapter


CONSUMER_NAME = "kis-broker-adapter"


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


def _kafka_error_code(exc: KafkaException) -> int | None:
    if not exc.args:
        return None
    error = exc.args[0]
    code = getattr(error, "code", None)
    if not callable(code):
        return None
    return code()


def _waitable_kafka_error_codes() -> set[int]:
    names = ("UNKNOWN_TOPIC_OR_PART", "_ALL_BROKERS_DOWN", "_TRANSPORT", "_TIMED_OUT")
    return {getattr(KafkaError, name) for name in names if hasattr(KafkaError, name)}


def _is_waitable_kafka_exception(exc: KafkaException) -> bool:
    code = _kafka_error_code(exc)
    return code in _waitable_kafka_error_codes()


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
        event_id = str(payload.get("event_id") or payload.get("eventId") or "").strip()
        repository = self.adapter.repository
        if event_id and repository.inbox_event_seen(CONSUMER_NAME, event_id):
            result = BrokerProcessResult(
                order_id=str(payload.get("order_id") or payload.get("orderId") or "") or None,
                status="DUPLICATE",
                skipped_external_submit=True,
                reason="Kafka event already processed",
            )
            self.consumer.commit(message=message, asynchronous=False)
            return result
        result = self.adapter.process_message(payload)
        if event_id:
            digest = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            repository.record_inbox_event(CONSUMER_NAME, event_id, payload_digest=digest)
        self.consumer.commit(message=message, asynchronous=False)
        return result

    def run(
        self,
        *,
        max_messages: int | None = None,
        timeout_seconds: float = 1.0,
        retry_kafka_errors: bool | None = None,
    ) -> int:
        processed = 0
        retry_kafka_errors = max_messages is None if retry_kafka_errors is None else retry_kafka_errors
        retry_seconds = float(os.getenv("KIS_ADAPTER_KAFKA_RETRY_SECONDS", "5"))
        touch_heartbeat()
        try:
            while max_messages is None or processed < max_messages:
                touch_heartbeat()
                try:
                    result = self.consume_once(timeout_seconds=timeout_seconds)
                except KafkaException as exc:
                    if not retry_kafka_errors or not _is_waitable_kafka_exception(exc):
                        raise
                    print(f"KIS broker adapter waiting for Kafka topic/bootstrap: {exc}", flush=True)
                    time.sleep(retry_seconds)
                    continue
                if result is None:
                    if max_messages is not None:
                        break
                    continue
                processed += 1
        finally:
            self.consumer.close()
        return processed
