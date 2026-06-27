from unittest.mock import patch

import pytest

from kis_trader.domain.status import OrderStatus
from kis_trader.outbox.producer import KafkaJsonProducer, RecordingProducer
from kis_trader.outbox.publisher import publish_pending_outbox

from test.fixtures.orders import repository_with_received_order


def test_command_outbox_publish_marks_published_and_records_status_event():
    repo, _envelope, _command = repository_with_received_order()
    producer = RecordingProducer()

    count = publish_pending_outbox(repo, producer)

    assert count == 1
    assert producer.messages[0]["topic"] == "orders.commands.v1"
    assert producer.messages[0]["key"] == "demo-account:AAPL"
    assert repo.fetch_pending_outbox() == []
    assert repo.get_order("ord-1")["status"] == OrderStatus.PUBLISHED.value
    assert [event["status"] for event in repo.list_order_events("ord-1")] == ["RECEIVED", "PUBLISHED"]


def test_publish_error_keeps_outbox_unpublished():
    repo, _envelope, _command = repository_with_received_order()

    class FailingProducer:
        def produce(self, *_args, **_kwargs):
            raise RuntimeError("produce failed")

    with pytest.raises(RuntimeError):
        publish_pending_outbox(repo, FailingProducer())

    assert len(repo.fetch_pending_outbox()) == 1
    assert repo.get_order("ord-1")["status"] == OrderStatus.RECEIVED.value


def test_kafka_json_producer_uses_idempotent_delivery_settings():
    created_configs = []
    produced_messages = []

    class FakeProducer:
        def __init__(self, config):
            created_configs.append(config)

        def produce(self, topic, key, value, callback):
            produced_messages.append({"topic": topic, "key": key, "value": value})
            callback(None, object())

        def flush(self):
            return None

    with patch("kis_trader.outbox.producer.ConfluentProducer", FakeProducer):
        producer = KafkaJsonProducer("localhost:29092")
        producer.produce("topic-1", "key-1", {"hello": "world"})

    assert created_configs[0]["acks"] == "all"
    assert created_configs[0]["enable.idempotence"] is True
    assert created_configs[0]["message.timeout.ms"] == 10000
    assert produced_messages[0]["key"] == b"key-1"
