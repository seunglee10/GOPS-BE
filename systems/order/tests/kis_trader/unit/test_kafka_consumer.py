import json

import pytest

from kis_trader.broker_adapter.adapter import KisBrokerAdapter
from kis_trader.broker_adapter.consumer import KafkaBrokerAdapterConsumer, build_broker_adapter_consumer_config
from kis_trader.kis.fake import FakeKisClient

from systems.order.tests.kis_trader.fixtures.orders import repository_with_published_order


class FakeKafkaMessage:
    def __init__(self, payload):
        self._payload = payload

    def error(self):
        return None

    def value(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeKafkaError:
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def __str__(self):
        return f"fake kafka error {self._code}"


class FakeKafkaErrorMessage:
    def __init__(self, error):
        self._error = error

    def error(self):
        return self._error


class FakeKafkaConsumer:
    def __init__(self, messages):
        self.messages = list(messages)
        self.subscribed_topics = None
        self.committed_messages = []
        self.closed = False

    def subscribe(self, topics):
        self.subscribed_topics = topics

    def poll(self, _timeout):
        if not self.messages:
            return None
        return self.messages.pop(0)

    def commit(self, message, asynchronous):
        self.committed_messages.append((message, asynchronous))

    def close(self):
        self.closed = True


def test_broker_adapter_consumer_group_and_manual_commit_are_fixed():
    config = build_broker_adapter_consumer_config("localhost:9092", **{"group.id": "wrong", "enable.auto.commit": True})

    assert config["bootstrap.servers"] == "localhost:9092"
    assert config["group.id"] == "kis-broker-adapter"
    assert config["enable.auto.commit"] is False


def test_consumer_commits_after_adapter_success():
    repo, envelope, _command = repository_with_published_order()
    adapter = KisBrokerAdapter(repo, FakeKisClient(["success"]))
    message = FakeKafkaMessage(envelope)
    consumer = FakeKafkaConsumer([message])
    runner = KafkaBrokerAdapterConsumer(consumer, adapter)

    processed = runner.run(max_messages=1)

    assert processed == 1
    assert consumer.subscribed_topics == ["orders.commands.v1"]
    assert consumer.committed_messages == [(message, False)]
    assert consumer.closed is True
    assert repo.get_order("ord-1")["status"] == "SUBMITTED"


def test_long_running_consumer_waits_for_missing_topic(monkeypatch):
    from confluent_kafka import KafkaError

    repo, envelope, _command = repository_with_published_order()
    adapter = KisBrokerAdapter(repo, FakeKisClient(["success"]))
    missing_topic = FakeKafkaErrorMessage(FakeKafkaError(KafkaError.UNKNOWN_TOPIC_OR_PART))
    message = FakeKafkaMessage(envelope)
    consumer = FakeKafkaConsumer([missing_topic, message])
    runner = KafkaBrokerAdapterConsumer(consumer, adapter)
    monkeypatch.setenv("KIS_ADAPTER_KAFKA_RETRY_SECONDS", "0")

    processed = runner.run(max_messages=1, retry_kafka_errors=True)

    assert processed == 1
    assert consumer.committed_messages == [(message, False)]
    assert consumer.closed is True


def test_consumer_factory_rejects_real_env_even_with_fake_kis(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "real")

    with pytest.raises(RuntimeError, match="KIS_ENV=real"):
        KafkaBrokerAdapterConsumer.from_env(fake_outcome="success")
