"""CLI for API, migrations, outbox publishing, adapter, reconciliation, and smoke checks."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Sequence

from kis_trader.broker_adapter.adapter import KisBrokerAdapter
from kis_trader.broker_adapter.consumer import KafkaBrokerAdapterConsumer
from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.domain.envelope import build_order_command_envelope, validate_order_envelope
from kis_trader.kis.fake import FakeKisClient
from kis_trader.operations.metrics import alert_conditions
from kis_trader.outbox.producer import KafkaJsonProducer, RecordingProducer
from kis_trader.outbox.publisher import publish_pending_outbox
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.persistence.migrations import reset_public_schema, run_migrations
from kis_trader.persistence.postgres import PostgresOrderRepository
from kis_trader.persistence.repository import OrderRepository
from kis_trader.reconciliation.reconciler import build_reconciliation_report
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kis-overseas")
    subcommands = parser.add_subparsers(dest="command", required=True)

    api = subcommands.add_parser("api", help="run FastAPI server")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)

    subcommands.add_parser("migrate", help="run explicit SQL migrations")
    reset = subcommands.add_parser("reset-test-db", help="drop and recreate public schema")
    reset.add_argument("--yes", action="store_true")

    outbox = subcommands.add_parser("outbox-publish", help="publish pending outbox events")
    outbox.add_argument("--producer", choices=["real", "fake"], default="real")
    outbox.add_argument("--limit", type=int, default=None)
    outbox.add_argument("--topic", default=None)

    adapter = subcommands.add_parser("broker-adapter", help="consume orders.commands.v1 and submit to KIS demo")
    adapter.add_argument("--max-messages", type=int, default=None)
    adapter.add_argument("--timeout-seconds", type=float, default=1.0)
    adapter.add_argument("--fake-kis", choices=["success", "reject", "timeout", "connection_reset", "safe_429", "unsafe_5xx"], default=None)

    reconcile = subcommands.add_parser("reconcile", help="run reconciliation from JSON rows")
    reconcile.add_argument("--rows-json", default="[]")

    subcommands.add_parser("ops-metrics", help="print order-path metrics")

    smoke = subcommands.add_parser("smoke", help="run in-memory fake order smoke")
    smoke.add_argument("--outcome", choices=["success", "reject", "timeout", "connection_reset"], default="success")

    args = parser.parse_args(argv)

    if args.command == "api":
        import uvicorn

        uvicorn.run("kis_trader.api.app:app", host=args.host, port=args.port)
        return 0
    if args.command == "migrate":
        applied = run_migrations(_database_url())
        print(json.dumps({"applied": applied}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "reset-test-db":
        if not args.yes:
            raise SystemExit("reset-test-db requires --yes")
        reset_public_schema(_database_url())
        print(json.dumps({"reset": True}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "outbox-publish":
        repository = _postgres_repository()
        producer = KafkaJsonProducer.from_env() if args.producer == "real" else RecordingProducer()
        count = publish_pending_outbox(repository, producer, limit=args.limit, topic=args.topic)
        print(json.dumps({"published_count": count, "messages": getattr(producer, "messages", [])}, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    if args.command == "broker-adapter":
        consumer = KafkaBrokerAdapterConsumer.from_env(fake_outcome=args.fake_kis)
        count = consumer.run(max_messages=args.max_messages, timeout_seconds=args.timeout_seconds)
        print(json.dumps({"processed_count": count}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "reconcile":
        repository = _postgres_repository()
        rows = json.loads(args.rows_json)
        report = build_reconciliation_report(repository, rows)
        print(json.dumps({"results": [r.__dict__ for r in report.results], "alerts": [a.__dict__ for a in report.alerts]}, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    if args.command == "ops-metrics":
        metrics = _postgres_repository().metrics_snapshot()
        print(json.dumps({"metrics": metrics, "alerts": alert_conditions(metrics)}, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    if args.command == "smoke":
        print(json.dumps(run_smoke(args.outcome), ensure_ascii=False, sort_keys=True, default=str))
        return 0
    return 2


def run_smoke(outcome: str = "success") -> dict[str, Any]:
    repository = InMemoryOrderRepository()
    request_payload = {
        "account_alias": "demo-account",
        "market": "overseas",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "price": "145.00",
        "exchange": "NASD",
        "order_division": "00",
    }
    request = validate_order_request_payload(request_payload, default_account_alias="demo-account")
    envelope = build_order_command_envelope(
        request,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        request_id="req-smoke",
        order_id="ord-smoke",
        client_order_id="coid-smoke",
        event_id="evt-smoke",
    )
    command = validate_order_envelope(envelope)
    repository.create_received_order(
        idempotency_key_hash=hash_idempotency_key("smoke-key", "smoke-secret"),
        body_hash=stable_body_hash(request_payload),
        command=command,
    )
    producer = RecordingProducer()
    publish_pending_outbox(repository, producer)
    adapter = KisBrokerAdapter(repository, FakeKisClient([outcome]))
    result = adapter.process_message(envelope)
    publish_pending_outbox(repository, producer)
    return {
        "order_id": result.order_id,
        "status": result.status,
        "published_topics": [message["topic"] for message in producer.messages],
        "metrics": repository.metrics_snapshot(),
    }


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url


def _postgres_repository() -> OrderRepository:
    return PostgresOrderRepository(_database_url())


if __name__ == "__main__":
    raise SystemExit(main())
