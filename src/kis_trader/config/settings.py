from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when runtime configuration is invalid."""


@dataclass(frozen=True)
class AppSettings:
    env: str
    database_url: str
    kafka_bootstrap_servers: str
    kafka_order_commands_topic: str
    kafka_submit_results_topic: str
    kafka_order_events_topic: str
    kafka_dlq_topic: str
    kafka_account_alias: str
    idempotency_hash_secret: str
    api_host: str
    api_port: int
    kafka_message_timeout_ms: int


def load_settings(
    *,
    env: str | None = None,
    env_file: str | Path | None = None,
) -> AppSettings:
    if env_file is not None:
        load_dotenv(env_file)
    else:
        load_dotenv()

    selected_env = (env or os.getenv("KIS_ENV", "demo")).strip().lower()
    if selected_env not in {"demo", "real"}:
        raise ConfigError("KIS_ENV must be either 'demo' or 'real'.")

    return AppSettings(
        env=selected_env,
        database_url=_env(
            "DATABASE_URL",
            "postgresql://gops:gops_dev_password@localhost:5433/gops",
        ),
        kafka_bootstrap_servers=_env("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092"),
        kafka_order_commands_topic=_env("KAFKA_ORDER_COMMANDS_TOPIC", "orders.commands.v1"),
        kafka_submit_results_topic=_env("KAFKA_SUBMIT_RESULTS_TOPIC", "broker.submit-results.v1"),
        kafka_order_events_topic=_env("KAFKA_ORDER_EVENTS_TOPIC", "broker.order-events.v1"),
        kafka_dlq_topic=_env("KAFKA_DLQ_TOPIC", "orders.dlq.v1"),
        kafka_account_alias=_env("KAFKA_ACCOUNT_ALIAS", f"{selected_env}-account"),
        idempotency_hash_secret=_env("IDEMPOTENCY_HASH_SECRET", "dev-only-idempotency-secret"),
        api_host=_env("GOPS_ORDER_API_HOST", "127.0.0.1"),
        api_port=_env_int("GOPS_ORDER_API_PORT", 8000),
        kafka_message_timeout_ms=_env_int("KAFKA_MESSAGE_TIMEOUT_MS", 10000),
    )


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
