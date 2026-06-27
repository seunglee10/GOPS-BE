import json
import os
import time
from uuid import uuid4

import pytest
from confluent_kafka import Consumer as ConfluentConsumer, KafkaException

from kis_trader.broker_adapter.adapter import KisBrokerAdapter
from kis_trader.domain.status import OrderStatus
from kis_trader.domain.topics import ORDERS_COMMANDS_TOPIC, SUBMIT_RESULTS_TOPIC
from kis_trader.kis.fake import FakeKisClient
from kis_trader.outbox.producer import KafkaJsonProducer
from kis_trader.outbox.publisher import publish_pending_outbox
from kis_trader.persistence.migrations import reset_public_schema, run_migrations
from kis_trader.persistence.postgres import PostgresOrderRepository
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash

from test.fixtures.orders import sample_command, sample_order_request


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_KAFKA_INTEGRATION") != "1"
    or not os.getenv("DATABASE_URL")
    or not os.getenv("KAFKA_BOOTSTRAP_SERVERS"),
    reason="set RUN_KAFKA_INTEGRATION=1, DATABASE_URL, and KAFKA_BOOTSTRAP_SERVERS to run Kafka integration tests",
)


def test_postgres_kafka_outbox_adapter_result_flow():
    conninfo = os.environ["DATABASE_URL"]
    bootstrap = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    reset_public_schema(conninfo)
    run_migrations(conninfo)
    repo = PostgresOrderRepository(conninfo)
    command = sample_command()
    producer = KafkaJsonProducer(bootstrap)

    consumer = ConfluentConsumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"test-kis-adapter-{uuid4().hex}",
            "enable.auto.commit": False,
            "auto.offset.reset": "latest",
        }
    )
    consumer.subscribe([ORDERS_COMMANDS_TOPIC])
    consumer.poll(1.0)

    repo.create_received_order(
        idempotency_key_hash=hash_idempotency_key("idem-1", "secret"),
        body_hash=stable_body_hash(sample_order_request()),
        command=command,
    )
    assert publish_pending_outbox(repo, producer, topic=ORDERS_COMMANDS_TOPIC) == 1

    adapter = KisBrokerAdapter(repo, FakeKisClient(["success"]))
    matched = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise KafkaException(message.error())
            payload = json.loads(message.value().decode("utf-8"))
            if payload.get("event_id") != command.event_id:
                consumer.commit(message=message, asynchronous=False)
                continue
            result = adapter.process_message(payload)
            consumer.commit(message=message, asynchronous=False)
            assert result.status == OrderStatus.SUBMITTED.value
            matched = True
            break
    finally:
        consumer.close()

    assert matched is True
    assert repo.get_order(command.order_id)["status"] == OrderStatus.SUBMITTED.value
    assert publish_pending_outbox(repo, producer, topic=SUBMIT_RESULTS_TOPIC) == 1
    assert repo.fetch_pending_outbox(topic=SUBMIT_RESULTS_TOPIC) == []
