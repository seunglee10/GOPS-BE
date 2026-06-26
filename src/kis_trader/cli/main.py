from __future__ import annotations

import argparse
import json
from typing import Any

from kis_trader.config import ConfigError, load_settings
from kis_trader.orders.postgres_repository import PostgresOrderRepository
from kis_trader.outbox import KafkaJsonProducer, OutboxPublisherService


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.handler(args)
    except (ConfigError, RuntimeError, ValueError, LookupError) as exc:
        parser.exit(2, f"error: {exc}\n")
    if payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GOPS order reliability minimal tools")
    parser.add_argument("--env", choices=["demo", "real"], default=None)
    parser.add_argument("--env-file", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_init = subparsers.add_parser("db-init", help="Create M1/M2/M4 PostgreSQL tables")
    db_init.set_defaults(handler=handle_db_init)

    api = subparsers.add_parser("api", help="Run the minimal FastAPI order API")
    api.add_argument("--host", default=None)
    api.add_argument("--port", type=int, default=None)
    api.set_defaults(handler=handle_api)

    outbox = subparsers.add_parser("outbox-publish", help="Publish pending outbox events to Kafka")
    outbox.add_argument("--limit", type=int, default=100)
    outbox.set_defaults(handler=handle_outbox_publish)

    return parser


def handle_db_init(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(env=args.env, env_file=args.env_file)
    repository = PostgresOrderRepository(settings.database_url)
    repository.ensure_schema()
    return {"database_initialized": True, "database_url": _redact_database_url(settings.database_url)}


def handle_api(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is not installed. Run uv sync first.") from exc

    from kis_trader.api import create_app

    settings = load_settings(env=args.env, env_file=args.env_file)
    uvicorn.run(
        create_app(settings=settings),
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
    )
    return None


def handle_outbox_publish(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(env=args.env, env_file=args.env_file)
    repository = PostgresOrderRepository(settings.database_url)
    producer = KafkaJsonProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        message_timeout_ms=settings.kafka_message_timeout_ms,
    )
    summary = OutboxPublisherService(storage=repository, producer=producer).publish_pending(limit=args.limit)
    return {"scanned": summary.scanned, "published": summary.published}


def _redact_database_url(value: str) -> str:
    if "://" not in value or "@" not in value:
        return value
    scheme, rest = value.split("://", 1)
    return f"{scheme}://[REDACTED]@{rest.split('@', 1)[1]}"
