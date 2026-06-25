from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required KIS configuration is missing or invalid."""


@dataclass(frozen=True)
class KisConfig:
    env: str
    app_key: str
    app_secret: str
    account_no: str
    product_code: str
    hts_id: str
    base_url: str
    token_cache_path: Path
    user_agent: str
    contact_phone: str
    mgco_aptm_odno: str
    order_server_code: str
    default_exchange: str
    default_currency: str
    timeout_seconds: float
    database_url: str
    kafka_bootstrap_servers: str
    kafka_order_commands_topic: str
    kafka_submit_results_topic: str
    kafka_order_events_topic: str
    kafka_reconciled_topic: str
    kafka_dlq_topic: str
    kafka_broker_adapter_group_id: str
    kafka_account_alias: str


def load_config(
    env: str | None = None,
    env_file: str | Path | None = None,
    *,
    require_kis_credentials: bool = True,
) -> KisConfig:
    if env_file is not None:
        load_dotenv(env_file)
    else:
        load_dotenv()

    selected_env = (env or os.getenv("KIS_ENV", "demo")).strip().lower()
    if selected_env not in {"demo", "real"}:
        raise ConfigError("KIS_ENV must be either 'demo' or 'real'.")

    prefix = "KIS_DEMO" if selected_env == "demo" else "KIS_REAL"
    base_url_key = "KIS_DEMO_BASE_URL" if selected_env == "demo" else "KIS_REAL_BASE_URL"

    required = {
        f"{prefix}_APP_KEY": os.getenv(f"{prefix}_APP_KEY", "").strip(),
        f"{prefix}_APP_SECRET": os.getenv(f"{prefix}_APP_SECRET", "").strip(),
        f"{prefix}_ACCOUNT_NO": os.getenv(f"{prefix}_ACCOUNT_NO", "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if require_kis_credentials and missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))

    timeout = os.getenv("KIS_TIMEOUT_SECONDS", "10").strip()
    try:
        timeout_seconds = float(timeout)
    except ValueError as exc:
        raise ConfigError("KIS_TIMEOUT_SECONDS must be a number.") from exc

    token_cache_path = Path(os.getenv("KIS_TOKEN_CACHE_PATH", ".kis_token_cache.json")).expanduser()

    return KisConfig(
        env=selected_env,
        app_key=required[f"{prefix}_APP_KEY"],
        app_secret=required[f"{prefix}_APP_SECRET"],
        account_no=required[f"{prefix}_ACCOUNT_NO"],
        product_code=os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01").strip() or "01",
        hts_id=os.getenv("KIS_HTS_ID", "").strip(),
        base_url=os.getenv(base_url_key, "").strip()
        or (
            "https://openapivts.koreainvestment.com:29443"
            if selected_env == "demo"
            else "https://openapi.koreainvestment.com:9443"
        ),
        token_cache_path=token_cache_path,
        user_agent=os.getenv(
            "KIS_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        ).strip(),
        contact_phone=os.getenv("KIS_CONTACT_PHONE", "").strip(),
        mgco_aptm_odno=os.getenv("KIS_MGCO_APTM_ODNO", "").strip(),
        order_server_code=os.getenv("KIS_ORDER_SERVER_CODE", "0").strip() or "0",
        default_exchange=os.getenv("KIS_DEFAULT_EXCHANGE", "NASD").strip().upper() or "NASD",
        default_currency=os.getenv("KIS_DEFAULT_CURRENCY", "USD").strip().upper() or "USD",
        timeout_seconds=timeout_seconds,
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://gops:gops_dev_password@localhost:5433/gops",
        ).strip()
        or "postgresql://gops:gops_dev_password@localhost:5433/gops",
        kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092").strip()
        or "localhost:29092",
        kafka_order_commands_topic=os.getenv("KAFKA_ORDER_COMMANDS_TOPIC", "orders.commands.v1").strip()
        or "orders.commands.v1",
        kafka_submit_results_topic=os.getenv("KAFKA_SUBMIT_RESULTS_TOPIC", "broker.submit-results.v1").strip()
        or "broker.submit-results.v1",
        kafka_order_events_topic=os.getenv("KAFKA_ORDER_EVENTS_TOPIC", "broker.order-events.v1").strip()
        or "broker.order-events.v1",
        kafka_reconciled_topic=os.getenv("KAFKA_RECONCILED_TOPIC", "orders.reconciled.v1").strip()
        or "orders.reconciled.v1",
        kafka_dlq_topic=os.getenv("KAFKA_DLQ_TOPIC", "orders.dlq.v1").strip() or "orders.dlq.v1",
        kafka_broker_adapter_group_id=os.getenv("KAFKA_BROKER_ADAPTER_GROUP_ID", "kis-broker-adapter").strip()
        or "kis-broker-adapter",
        kafka_account_alias=os.getenv("KAFKA_ACCOUNT_ALIAS", f"{selected_env}-account").strip()
        or f"{selected_env}-account",
    )
