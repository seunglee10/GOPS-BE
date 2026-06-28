import os

import pytest

from kis_trader.domain.status import OrderStatus
from kis_trader.outbox.producer import RecordingProducer
from kis_trader.outbox.publisher import publish_pending_outbox
from kis_trader.persistence.migrations import reset_public_schema, run_migrations
from kis_trader.persistence.postgres import PostgresOrderRepository
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash

from tests.kis_trader.fixtures.orders import sample_command, sample_order_request


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION") != "1" or not os.getenv("DATABASE_URL"),
    reason="set RUN_DB_INTEGRATION=1 and DATABASE_URL to run Postgres integration tests",
)


def test_postgres_order_accept_and_outbox_transaction():
    conninfo = os.environ["DATABASE_URL"]
    reset_public_schema(conninfo)
    run_migrations(conninfo)
    repo = PostgresOrderRepository(conninfo)
    command = sample_command()

    result = repo.create_received_order(
        idempotency_key_hash=hash_idempotency_key("idem-1", "secret"),
        body_hash=stable_body_hash(sample_order_request()),
        command=command,
    )

    assert result.response["status"] == OrderStatus.RECEIVED.value
    assert repo.get_order("ord-1")["status"] == OrderStatus.RECEIVED.value
    assert repo.fetch_pending_outbox()[0]["topic"] == "orders.commands.v1"

    count = publish_pending_outbox(repo, RecordingProducer())

    assert count == 1
    assert repo.get_order("ord-1")["status"] == OrderStatus.PUBLISHED.value
