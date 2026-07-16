import json
import os
import time
from decimal import Decimal
from uuid import uuid4

import pytest
from confluent_kafka import Consumer as ConfluentConsumer, KafkaException

from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.outbox.producer import KafkaJsonProducer
from kis_trader.paper.matcher import match_quote_payload
from kis_trader.paper.postgres import PostgresPaperTradingRepository
from kis_trader.persistence.migrations import reset_public_schema, run_migrations


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_KAFKA_INTEGRATION") != "1"
    or not os.getenv("DATABASE_URL")
    or not os.getenv("KAFKA_BOOTSTRAP_SERVERS"),
    reason="set RUN_KAFKA_INTEGRATION=1, DATABASE_URL, and KAFKA_BOOTSTRAP_SERVERS to run paper Kafka integration tests",
)


def test_kafka_quote_fills_persistent_paper_account():
    conninfo = os.environ["DATABASE_URL"]
    bootstrap = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    topic = os.getenv("PAPER_ORDER_QUOTES_TOPIC", "market.layer.quotes.v1")
    event_id = f"paper-quote-{uuid4().hex}"
    reset_public_schema(conninfo)
    run_migrations(conninfo)
    repository = PostgresPaperTradingRepository(conninfo)
    request = validate_order_request_payload(
        {
            "market": "overseas",
            "symbol": "AAPL",
            "side": "buy",
            "qty": "2",
            "price": "100",
            "exchange": "NASD",
            "order_division": "00",
        },
        default_account_alias="paper-account",
    )
    repository.create_order(
        user_id="paper-kafka-user",
        idempotency_key_hash="paper-kafka-order",
        body_hash="paper-kafka-order",
        request=request,
    )

    consumer = ConfluentConsumer({
        "bootstrap.servers": bootstrap,
        "group.id": f"test-paper-matcher-{uuid4().hex}",
        "enable.auto.commit": False,
        "auto.offset.reset": "latest",
    })
    consumer.subscribe([topic])
    consumer.poll(1.0)
    KafkaJsonProducer(bootstrap).produce(topic, "AAPL", {
        "eventType": "QUOTE",
        "symbol": "AAPL",
        "bidPrice": 98.5,
        "askPrice": 99.5,
        "timestamp": "2026-07-14T10:00:00Z",
        "sourceEventId": event_id,
        "marketSession": "extended",
    })

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
            if payload.get("sourceEventId") != event_id:
                consumer.commit(message=message, asynchronous=False)
                continue
            fills = match_quote_payload(repository, payload)
            consumer.commit(message=message, asynchronous=False)
            assert fills[0]["fill_price"] == Decimal("99.5")
            matched = True
            break
    finally:
        consumer.close()

    assert matched is True
    snapshot = repository.account_snapshot("paper-kafka-user")
    assert snapshot["positions"][0]["qty"] == Decimal("2")
    assert snapshot["account"]["cash_balance"] == Decimal("99801")
