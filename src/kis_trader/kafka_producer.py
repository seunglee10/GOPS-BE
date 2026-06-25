from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .config import KisConfig
from .models import DomesticOrderRequest, OverseasOrderRequest


class KafkaPublishError(RuntimeError):
    """Raised when an order command cannot be written to Kafka."""


@dataclass(frozen=True)
class KafkaPublishResult:
    accepted_to_kafka: bool
    topic: str
    key: str
    request_id: str
    event_id: str
    partition: int | None
    offset: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_to_kafka": self.accepted_to_kafka,
            "topic": self.topic,
            "key": self.key,
            "request_id": self.request_id,
            "event_id": self.event_id,
            "partition": self.partition,
            "offset": self.offset,
        }


def publish_domestic_order_command(config: KisConfig, order_request: DomesticOrderRequest) -> KafkaPublishResult:
    payload = {
        "market": "domestic",
        "symbol": order_request.symbol,
        "side": order_request.side,
        "qty": _decimal_to_string(order_request.quantity),
        "price": _decimal_to_string(order_request.price),
        "exchange": order_request.exchange,
        "order_division": order_request.order_division,
        "sell_type": order_request.sell_type,
        "condition_price": order_request.condition_price,
    }
    return _publish_order_command(config, payload=payload, symbol=order_request.symbol)


def publish_overseas_order_command(config: KisConfig, order_request: OverseasOrderRequest) -> KafkaPublishResult:
    payload = {
        "market": "overseas",
        "symbol": order_request.symbol,
        "side": order_request.side,
        "qty": _decimal_to_string(order_request.quantity),
        "price": _decimal_to_string(order_request.price),
        "exchange": order_request.exchange,
        "order_division": order_request.order_division,
    }
    return _publish_order_command(config, payload=payload, symbol=order_request.symbol)


def _publish_order_command(config: KisConfig, *, payload: dict[str, Any], symbol: str) -> KafkaPublishResult:
    topic = config.kafka_order_commands_topic
    key = f"{config.kafka_account_alias}:{symbol}"
    event_id = str(uuid4())
    request_id = str(uuid4())
    event = {
        "schema_version": 1,
        "event_type": "order.submit.requested",
        "event_id": event_id,
        "request_id": request_id,
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "producer": "kis-trader-cli",
        "env": config.env,
        "account_alias": config.kafka_account_alias,
        "payload": payload,
    }

    partition, offset = _produce_json(
        bootstrap_servers=config.kafka_bootstrap_servers,
        topic=topic,
        key=key,
        value=event,
    )
    return KafkaPublishResult(
        accepted_to_kafka=True,
        topic=topic,
        key=key,
        request_id=request_id,
        event_id=event_id,
        partition=partition,
        offset=offset,
    )


def _produce_json(
    *,
    bootstrap_servers: str,
    topic: str,
    key: str,
    value: dict[str, Any],
    timeout_seconds: float = 10.0,
) -> tuple[int | None, int | None]:
    try:
        from confluent_kafka import KafkaException, Producer
    except ImportError as exc:
        raise KafkaPublishError("confluent-kafka is not installed. Rebuild or sync dependencies first.") from exc

    producer = Producer(
        {
            "bootstrap.servers": bootstrap_servers,
            "acks": "all",
            "enable.idempotence": True,
        }
    )
    encoded_value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    delivered: dict[str, int | None] = {"partition": None, "offset": None}
    delivery_error: KafkaPublishError | None = None

    def on_delivery(error: Any, message: Any) -> None:
        nonlocal delivery_error
        if error is not None:
            delivery_error = KafkaPublishError(f"Kafka delivery failed: {error}")
            return
        delivered["partition"] = message.partition()
        delivered["offset"] = message.offset()

    try:
        producer.produce(topic=topic, key=key.encode("utf-8"), value=encoded_value, on_delivery=on_delivery)
        remaining = producer.flush(timeout_seconds)
    except KafkaException as exc:
        raise KafkaPublishError(f"Kafka produce failed: {exc}") from exc

    if remaining:
        raise KafkaPublishError(f"Kafka delivery timed out with {remaining} message(s) still queued.")
    if delivery_error is not None:
        raise delivery_error
    return delivered["partition"], delivered["offset"]


def _decimal_to_string(value: Decimal) -> str:
    return format(value, "f")
