from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from confluent_kafka import Consumer
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import NewTopic

from kis_trader.api import create_app
from kis_trader.config import AppSettings, load_settings
from kis_trader.contracts import CANONICAL_TOPIC_NAMES, REDACTED, OrderStatus
from kis_trader.ledger import SubmissionInput, SubmissionLedgerService
from kis_trader.orders import PostgresOrderRepository
from kis_trader.outbox import KafkaJsonProducer, OutboxPublisherService


def sample_order(*, symbol: str = "AAPL", qty: str = "1", price: str = "145.00") -> dict[str, str]:
    return {
        "market": "overseas",
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "price": price,
        "exchange": "NASD",
        "order_division": "00",
    }


class ApiClient:
    def __init__(self, app: Any) -> None:
        self._app = app

    def post(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        return asyncio.run(self._request("POST", path, headers=headers, json=json))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers, json=json)


@pytest.fixture(scope="session")
def settings() -> AppSettings:
    resolved = load_settings()
    wait_for_postgres(resolved.database_url)
    wait_for_kafka(resolved.kafka_bootstrap_servers)
    create_topics(resolved)
    return resolved


@pytest.fixture()
def repository(settings: AppSettings) -> PostgresOrderRepository:
    repo = PostgresOrderRepository(settings.database_url)
    repo.ensure_schema()
    repo.truncate_for_tests()
    return repo


@pytest.fixture()
def client(settings: AppSettings, repository: PostgresOrderRepository) -> ApiClient:
    return ApiClient(create_app(settings=settings))


def test_post_orders_requires_idempotency_key(client: ApiClient) -> None:
    response = client.post("/orders", json=sample_order())

    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


def test_same_key_and_body_converges_to_one_order(
    client: ApiClient,
    repository: PostgresOrderRepository,
) -> None:
    order_ids = set()
    request_ids = set()
    for _ in range(100):
        response = client.post(
            "/orders",
            headers={"Idempotency-Key": "same-intent-key"},
            json=sample_order(),
        )
        assert response.status_code == 202
        payload = response.json()
        order_ids.add(payload["order_id"])
        request_ids.add(payload["request_id"])

    assert len(order_ids) == 1
    assert len(request_ids) == 1
    assert repository.count_rows("orders") == 1
    assert repository.count_rows("outbox_events") == 1

    pending = repository.fetch_pending_outbox(limit=10)
    serialized = json.dumps(pending[0].payload, ensure_ascii=False, sort_keys=True)
    assert "same-intent-key" not in serialized
    assert "CANO" not in serialized
    assert "access_token" not in serialized


def test_same_key_with_different_body_returns_conflict(client: ApiClient) -> None:
    first = client.post(
        "/orders",
        headers={"Idempotency-Key": "conflict-key"},
        json=sample_order(qty="1"),
    )
    second = client.post(
        "/orders",
        headers={"Idempotency-Key": "conflict-key"},
        json=sample_order(qty="2"),
    )

    assert first.status_code == 202
    assert second.status_code == 409


def test_outbox_publish_writes_order_command_to_kafka_and_marks_published(
    settings: AppSettings,
    client: ApiClient,
    repository: PostgresOrderRepository,
) -> None:
    accepted = client.post(
        "/orders",
        headers={"Idempotency-Key": "publish-command-key"},
        json=sample_order(symbol="MSFT", price="300.00"),
    ).json()
    pending = repository.fetch_pending_outbox(limit=10)
    command_event_id = pending[0].event_id

    summary = OutboxPublisherService(
        storage=repository,
        producer=KafkaJsonProducer(settings.kafka_bootstrap_servers, settings.kafka_message_timeout_ms),
    ).publish_pending(limit=10)

    assert summary.scanned == 1
    assert summary.published == 1
    published_order = repository.get_order(accepted["order_id"])
    assert published_order is not None
    assert published_order.status == OrderStatus.PUBLISHED

    kafka_value = wait_for_event(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topic=settings.kafka_order_commands_topic,
        event_id=command_event_id,
    )
    kafka_payload = _dict_value(kafka_value, "payload")
    assert kafka_value["event_type"] == "order.submit.requested"
    assert kafka_value["order_id"] == accepted["order_id"]
    assert kafka_value["client_order_id"] == accepted["client_order_id"]
    assert kafka_value["account_alias"] == settings.kafka_account_alias
    assert kafka_payload["symbol"] == "MSFT"
    assert "raw_idempotency_key" not in json.dumps(kafka_value, sort_keys=True)


def test_submission_result_ledger_records_and_publishes_submit_result(
    settings: AppSettings,
    client: ApiClient,
    repository: PostgresOrderRepository,
) -> None:
    accepted = client.post(
        "/orders",
        headers={"Idempotency-Key": "submission-result-key"},
        json=sample_order(symbol="NVDA", price="900.00"),
    ).json()
    OutboxPublisherService(
        storage=repository,
        producer=KafkaJsonProducer(settings.kafka_bootstrap_servers, settings.kafka_message_timeout_ms),
    ).publish_pending(limit=10)

    ledger = SubmissionLedgerService(settings=settings, storage=repository)
    result = ledger.record_submission_result(
        SubmissionInput(
            order_id=accepted["order_id"],
            request_id=accepted["request_id"],
            client_order_id=accepted["client_order_id"],
            account_alias=accepted["account_alias"],
            env=accepted["env"],
            market=accepted["market"],
            symbol=accepted["symbol"],
            side=accepted["side"],
            qty=accepted["qty"],
            price=accepted["price"],
            exchange=accepted["exchange"],
            status=OrderStatus.SUBMITTED,
            raw_request={
                "headers": {"authorization": "REDACT_ME_AUTHORIZATION"},
                "body": {"CANO": "REDACT_ME_ACCOUNT", "ACNT_PRDT_CD": "REDACT_ME_PRODUCT", "PDNO": "NVDA"},
            },
            raw_response={"rt_cd": "0", "msg_cd": "0000", "output": {"ODNO": "KIS-ORDER-1"}},
            http_status=200,
            kis_msg_cd="0000",
            kis_order_id="KIS-ORDER-1",
        )
    )

    duplicate = ledger.record_submission_result(
        SubmissionInput(
            order_id=accepted["order_id"],
            request_id=accepted["request_id"],
            client_order_id=accepted["client_order_id"],
            account_alias=accepted["account_alias"],
            env=accepted["env"],
            market=accepted["market"],
            symbol=accepted["symbol"],
            side=accepted["side"],
            qty=accepted["qty"],
            price=accepted["price"],
            exchange=accepted["exchange"],
            status=OrderStatus.SUBMITTED,
            raw_request={"body": {"CANO": "REDACT_ME_ACCOUNT"}},
            raw_response={"rt_cd": "0"},
            http_status=200,
            kis_order_id="KIS-ORDER-1",
        )
    )

    assert result.created is True
    assert duplicate.created is False
    assert repository.count_rows("broker_submissions") == 1
    submitted_order = repository.get_order(accepted["order_id"])
    assert submitted_order is not None
    assert submitted_order.status == OrderStatus.SUBMITTED

    submission = fetch_one_submission(settings.database_url)
    redacted_request = _dict_value(submission, "redacted_request")
    redacted_headers = _dict_value(redacted_request, "headers")
    redacted_body = _dict_value(redacted_request, "body")
    assert redacted_headers["authorization"] == REDACTED
    assert redacted_body["CANO"] == REDACTED
    assert redacted_body["ACNT_PRDT_CD"] == REDACTED

    pending = repository.fetch_pending_outbox(limit=10)
    result_event = next(event for event in pending if event.topic == settings.kafka_submit_results_topic)
    OutboxPublisherService(
        storage=repository,
        producer=KafkaJsonProducer(settings.kafka_bootstrap_servers, settings.kafka_message_timeout_ms),
    ).publish_pending(limit=10)

    kafka_value = wait_for_event(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topic=settings.kafka_submit_results_topic,
        event_id=result_event.event_id,
    )
    kafka_payload = _dict_value(kafka_value, "payload")
    assert kafka_value["event_type"] == "order.submitted"
    assert kafka_value["status"] == "SUBMITTED"
    assert kafka_payload["kis_order_id"] == "KIS-ORDER-1"
    assert "REDACT_ME_ACCOUNT" not in json.dumps(kafka_value, sort_keys=True)
    assert "REDACT_ME_AUTHORIZATION" not in json.dumps(kafka_value, sort_keys=True)


def test_contract_uses_m0_statuses_and_canonical_topics_only() -> None:
    assert "VALIDATED" not in {status.value for status in OrderStatus}
    assert CANONICAL_TOPIC_NAMES == {
        "orders.commands.v1",
        "broker.submit-results.v1",
        "broker.order-events.v1",
        "orders.dlq.v1",
    }

    source_root = Path(__file__).resolve().parents[1] / "src" / "kis_trader"
    source_text = "\n".join(path.read_text() for path in source_root.rglob("*.py"))
    assert "orders.reconciled.v1" not in source_text
    assert "VALIDATED" not in source_text


def wait_for_postgres(database_url: str) -> None:
    import psycopg

    deadline = time.time() + 60
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with psycopg.connect(database_url) as conn:
                conn.execute("SELECT 1")
                return
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"PostgreSQL did not become ready: {last_error}")


def wait_for_kafka(bootstrap_servers: str) -> None:
    deadline = time.time() + 90
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            metadata = admin.list_topics(timeout=5)
            if metadata.brokers:
                return
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"Kafka did not become ready: {last_error}")


def create_topics(settings: AppSettings) -> None:
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})
    topics = [
        NewTopic(settings.kafka_order_commands_topic, num_partitions=3, replication_factor=1),
        NewTopic(settings.kafka_submit_results_topic, num_partitions=3, replication_factor=1),
        NewTopic(settings.kafka_order_events_topic, num_partitions=3, replication_factor=1),
        NewTopic(settings.kafka_dlq_topic, num_partitions=3, replication_factor=1),
    ]
    futures = admin.create_topics(topics)
    for future in futures.values():
        try:
            future.result(timeout=30)
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise


def wait_for_event(*, bootstrap_servers: str, topic: str, event_id: str) -> dict[str, Any]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": f"gops-test-{uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    deadline = time.time() + 30
    try:
        consumer.subscribe([topic])
        while time.time() < deadline:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                continue
            raw_value = message.value()
            if raw_value is None:
                continue
            value = json.loads(raw_value.decode("utf-8"))
            if value.get("event_id") == event_id:
                return cast(dict[str, Any], value)
    finally:
        consumer.close()
    raise AssertionError(f"Kafka event {event_id} not found on {topic}")


def fetch_one_submission(database_url: str) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT redacted_request, redacted_response
            FROM broker_submissions
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return {"redacted_request": row[0], "redacted_response": row[1]}


def _dict_value(value: dict[str, Any], key: str) -> dict[str, Any]:
    item = value[key]
    assert isinstance(item, dict)
    return cast(dict[str, Any], item)
