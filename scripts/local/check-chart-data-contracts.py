#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOCAL_DDL = ROOT / "infra/clickhouse/initdb/01-market-data.sql"
K8S_DDL = ROOT / "infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql"
LOCAL_TOPICS = ROOT / "platform/kafka/topics.txt"
K8S_TOPICS = ROOT / "infra/k8s/base/platform/kafka/topics.txt"
ROOT_ENV_EXAMPLE = ROOT / ".env.example"
API_ENV_EXAMPLE = ROOT / "systems/api-server/.env.example"
ROOT_CHART_ENV = (
    "REDIS_URL",
    "REDIS_KEY_PREFIX",
    "S3_BUCKET",
    "S3_RAW_PREFIX",
    "S3_FINAL_PREFIX",
    "S3_MANIFEST_PREFIX",
    "S3_PROCESSED_FORMAT",
    "S3_REALTIME_LAYOUT_MODE",
    "S3_FLUSH_COUNT",
    "S3_FLUSH_INTERVAL_SECONDS",
    "S3_RAW_FLUSH_COUNT",
    "S3_RAW_FLUSH_INTERVAL_SECONDS",
    "S3_PUT_MAX_ATTEMPTS",
    "S3_PUT_RETRY_SLEEP_SECONDS",
    "ORDER_FLOW_PINNED_SYMBOLS",
    "ORDER_FLOW_PRICE_BIN_SIZE",
    "ORDER_FLOW_QUOTE_REFRESH_MS",
    "ORDER_FLOW_QUOTE_MAX_AGE_MS",
    "ORDER_FLOW_QUOTE_FUTURE_TOLERANCE_MS",
    "ORDER_FLOW_PUBLISH_THROTTLE_MS",
    "ORDER_FLOW_REDIS_FLUSH_MS",
    "ORDER_FLOW_LIVE_TTL_SECONDS",
    "ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS",
    "QUOTE_REDIS_WRITE_MIN_INTERVAL_MS",
    "QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS",
    "TRADE_REDIS_WRITE_MIN_INTERVAL_MS",
    "HEALTH_WRITE_MIN_INTERVAL_MS",
    "CHART_INDICATOR_CACHE_TTL_SECONDS",
    "CHART_VOLUME_PROFILE_CACHE_TTL_SECONDS",
    "CHART_DERIVED_INLINE_LOCK_TTL_SECONDS",
    "CHART_DERIVED_INLINE_WAIT_MS",
    "ON_DEMAND_FILL_TIMEOUT_SECONDS",
    "ON_DEMAND_FILL_BACKGROUND_ENABLED",
    "ON_DEMAND_FILL_BACKGROUND_WORKERS",
    "ON_DEMAND_FILL_BACKGROUND_TIMEOUT_SECONDS",
    "ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED",
    "ON_DEMAND_FILL_FOREGROUND_MAX_BARS",
    "ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS",
    "ON_DEMAND_FILL_FOREGROUND_AUTO_MAX_BARS",
    "ON_DEMAND_FILL_DISTRIBUTED_SINGLEFLIGHT_ENABLED",
    "ON_DEMAND_FILL_SINGLEFLIGHT_LOCK_TTL_SECONDS",
    "ON_DEMAND_FILL_SINGLEFLIGHT_TERMINAL_TTL_SECONDS",
)
COMPOSE_CHART_TUNABLES = tuple(
    name for name in ROOT_CHART_ENV if name not in {"REDIS_URL", "REDIS_KEY_PREFIX", "S3_PROCESSED_FORMAT"}
)
API_MIRRORED_ENV = (
    "REDIS_URL",
    "REDIS_KEY_PREFIX",
    "S3_BUCKET",
    "S3_FINAL_PREFIX",
    "S3_MANIFEST_PREFIX",
    "S3_PROCESSED_FORMAT",
    "ORDER_FLOW_PINNED_SYMBOLS",
    "ORDER_FLOW_PRICE_BIN_SIZE",
    "CHART_INDICATOR_CACHE_TTL_SECONDS",
    "CHART_VOLUME_PROFILE_CACHE_TTL_SECONDS",
    "CHART_DERIVED_INLINE_LOCK_TTL_SECONDS",
    "CHART_DERIVED_INLINE_WAIT_MS",
    "ON_DEMAND_FILL_TIMEOUT_SECONDS",
    "ON_DEMAND_FILL_BACKGROUND_ENABLED",
    "ON_DEMAND_FILL_BACKGROUND_WORKERS",
    "ON_DEMAND_FILL_BACKGROUND_TIMEOUT_SECONDS",
    "ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED",
    "ON_DEMAND_FILL_FOREGROUND_MAX_BARS",
    "ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS",
    "ON_DEMAND_FILL_FOREGROUND_AUTO_MAX_BARS",
    "ON_DEMAND_FILL_DISTRIBUTED_SINGLEFLIGHT_ENABLED",
    "ON_DEMAND_FILL_SINGLEFLIGHT_LOCK_TTL_SECONDS",
    "ON_DEMAND_FILL_SINGLEFLIGHT_TERMINAL_TTL_SECONDS",
)
TEXT_SCAN_EXCLUDED_DIRS = {
    ".git",
    ".terraform",
    ".venv",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def topic_values(path: Path) -> list[str]:
    return [
        line.strip()
        for line in read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def table_ddl(sql: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS market_data\.{re.escape(table)}\b.*?;",
        sql,
        flags=re.DOTALL,
    )
    return match.group(0) if match else ""


def normalized_market_ddl(sql: str) -> str:
    sql = re.sub(
        r"CREATE TABLE IF NOT EXISTS market_data\.agent_graph_expansions\b.*?;",
        "",
        sql,
        flags=re.DOTALL,
    )
    lines = [line.rstrip() for line in sql.splitlines() if not line.lstrip().startswith("--")]
    return "\n".join(line for line in lines if line.strip()).strip()


def env_csv(path: Path, name: str) -> list[str]:
    matches = re.findall(rf"^\s*{re.escape(name)}:\s*[\"']?([^\"'\n]+)", read(path), flags=re.MULTILINE)
    if not matches:
        return []
    return [item.strip() for item in matches[-1].split(",") if item.strip()]


def dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in read(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def compose_defaults(compose: str, name: str) -> set[str]:
    return set(re.findall(rf"\$\{{{re.escape(name)}:-([^}}]*)\}}", compose))


def terraform_variable_default(terraform: str, name: str) -> str | None:
    match = re.search(
        rf'variable "{re.escape(name)}" \{{(.*?)\n\}}',
        terraform,
        flags=re.DOTALL,
    )
    if not match:
        return None
    default = re.search(r"^\s*default\s*=\s*([^\s#]+)", match.group(1), flags=re.MULTILINE)
    return default.group(1) if default else None


def repository_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in TEXT_SCAN_EXCLUDED_DIRS for part in path.parts):
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in body:
            continue
        try:
            yield path, body.decode("utf-8")
        except UnicodeDecodeError:
            continue


def retired_contract_tokens() -> tuple[str, ...]:
    return (
        "volume" + "ProfileLiveKey",
        "artifact" + "Stored",
        "VOLUME_PROFILE_" + "LIVE_",
        "chart-derived-data-" + "worker",
        ".".join(("market", "chart-derived", "requests", "v1")),
        ".".join(("market", "chart-derived", "dlq", "v1")),
        "CHART_DERIVED_" + "REQUEST_TOPIC",
        "CHART_DERIVED_" + "DLQ_TOPIC",
        "CHART_DERIVED_" + "WORKER_GROUP_ID",
        "CHART_DATA_" + "REBUILD_PLAN.md",
        "CHART_DATA_" + "CONTRACTS.md",
        "alpaca-data-" + "pipeline-plan",
        "chart-data-" + "efficiency",
        "orderflow-bidask-" + "stabilization",
        "market-data-tick-candle-" + "architecture.md",
    )


def collect_errors() -> list[str]:
    errors: list[str] = []
    local_sql = read(LOCAL_DDL)
    k8s_sql = read(K8S_DDL)

    if normalized_market_ddl(local_sql) != normalized_market_ddl(k8s_sql):
        errors.append("ClickHouse init DDL copies differ outside declared headers/local-only agent table")

    tick_ttl = "TTL toDateTime(event_time) + INTERVAL 21 DAY DELETE"
    migration_tick_ttl = "TTL event_time + INTERVAL 21 DAY DELETE"
    for label, sql in (("local", local_sql), ("k8s", k8s_sql)):
        for table in ("trade_ticks", "quote_ticks"):
            if tick_ttl not in table_ddl(sql, table):
                errors.append(f"{label} {table} is missing the 21-day delete TTL")
        for table in ("chart_candles", "order_flow_profile_daily", "chart_analysis_assets"):
            ddl = table_ddl(sql, table)
            if not ddl:
                errors.append(f"{label} {table} DDL is missing")
            elif "TTL " in ddl:
                errors.append(f"{label} {table} must not have a deletion TTL")

    local_topics = topic_values(LOCAL_TOPICS)
    k8s_topics = topic_values(K8S_TOPICS)
    if local_topics != k8s_topics:
        errors.append("Kafka topic inventory copies differ")

    retired_topics = {
        ".".join(("market", "layer", "candles", "live", "v1")),
        ".".join(("market", "chart-derived", "requests", "v1")),
        ".".join(("market", "chart-derived", "dlq", "v1")),
    }
    present = retired_topics.intersection(local_topics)
    if present:
        errors.append(f"Retired Kafka topics remain in inventory: {sorted(present)}")

    compose = read(ROOT / "docker-compose.yml")
    local_topic_script = read(ROOT / "scripts/local/create-kafka-topics.sh")
    if "./platform/kafka/topics.txt:/etc/alfaka-kafka/topics.txt:ro" not in compose:
        errors.append("Docker Compose kafka-init does not mount the canonical topic inventory")
    if "done < /etc/alfaka-kafka/topics.txt" not in compose:
        errors.append("Docker Compose kafka-init does not read the canonical topic inventory")
    if 'done < "${TOPICS_FILE}"' not in local_topic_script:
        errors.append("Local Kafka topic creation does not read platform/kafka/topics.txt")

    root_env = dotenv_values(ROOT_ENV_EXAMPLE)
    api_env = dotenv_values(API_ENV_EXAMPLE)
    for name in ROOT_CHART_ENV:
        if name not in root_env:
            errors.append(f"Root .env.example is missing chart-data setting: {name}")
    for name in COMPOSE_CHART_TUNABLES:
        defaults = compose_defaults(compose, name)
        if not defaults:
            errors.append(f"Docker Compose does not forward chart-data setting: {name}")
        elif root_env.get(name) not in defaults:
            errors.append(
                f"Root .env.example default differs from Docker Compose for {name}: "
                f"example={root_env.get(name)!r} compose={sorted(defaults)!r}"
            )
    for name in API_MIRRORED_ENV:
        if api_env.get(name) != root_env.get(name):
            errors.append(
                f"API .env.example differs from root chart-data setting {name}: "
                f"api={api_env.get(name)!r} root={root_env.get(name)!r}"
            )

    expected_processed = env_csv(ROOT / "docker-compose.yml", "KAFKA_PROCESSED_TOPICS")
    for path in (
        ROOT / "infra/k8s/base/app/configmap.yaml",
        ROOT / "infra/k8s/overlays/aws/configmap-aws-patch.yaml",
    ):
        if env_csv(path, "KAFKA_PROCESSED_TOPICS") != expected_processed:
            errors.append(f"Processed S3 topic contract differs: {path.relative_to(ROOT)}")

    for path in (
        ROOT / "docker-compose.yml",
        ROOT / "infra/k8s/base/app/configmap.yaml",
        ROOT / "infra/k8s/overlays/aws/configmap-aws-patch.yaml",
    ):
        if not re.search(r'^\s*KAFKA_RAW_S3_ENABLE_AUTO_COMMIT:\s*["\']?false["\']?\s*$', read(path), flags=re.MULTILINE):
            errors.append(f"Raw S3 consumer must disable auto commit: {path.relative_to(ROOT)}")

    terraform = read(ROOT / "infra/aws/terraform/main.tf")
    terraform_variables = read(ROOT / "infra/aws/terraform/variables.tf")
    terraform_example = read(ROOT / "infra/aws/terraform/terraform.tfvars.example")
    lifecycle = re.search(
        r'resource "aws_s3_bucket_lifecycle_configuration" "market_data" \{.*?\n\}',
        terraform,
        flags=re.DOTALL,
    )
    lifecycle_body = lifecycle.group(0) if lifecycle else ""
    if not lifecycle_body:
        errors.append("S3 chart-data lifecycle resource is missing")
    else:
        for rule_id in ("expire-chart-raw-v1", "expire-chart-raw-v2"):
            if rule_id not in lifecycle_body:
                errors.append(f"S3 lifecycle rule is missing: {rule_id}")
        if "var.s3_raw_retention_days" not in lifecycle_body:
            errors.append("S3 lifecycle does not use the bounded raw retention variable")
        if "/final" in lifecycle_body:
            errors.append("Final candle evidence must not have an expiration rule")
        ownership_guard = "var.create_s3_bucket || var.acknowledge_s3_lifecycle_document_ownership"
        if ownership_guard not in lifecycle_body:
            errors.append("Existing S3 bucket lifecycle ownership acknowledgement guard is missing")

    if terraform_variable_default(terraform_variables, "manage_s3_chart_data_lifecycle") != "false":
        errors.append("Terraform must default S3 lifecycle management to false")
    if terraform_variable_default(terraform_variables, "acknowledge_s3_lifecycle_document_ownership") != "false":
        errors.append("Terraform must default lifecycle ownership acknowledgement to false")
    if not re.search(r"^manage_s3_chart_data_lifecycle\s*=\s*false$", terraform_example, flags=re.MULTILINE):
        errors.append("Terraform example must leave S3 lifecycle management disabled")

    retired_tokens = retired_contract_tokens()
    for path, body in repository_text_files():
        relative = path.relative_to(ROOT).as_posix()
        for token in retired_tokens:
            if token in relative or token in body:
                errors.append(f"Retired chart-data contract remains: {token} in {relative}")

    migration = read(ROOT / "scripts/local/migrate-chart-tick-retention.sql")
    for table in ("trade_ticks", "quote_ticks"):
        if f"ALTER TABLE market_data.{table}" not in migration or migration_tick_ttl not in migration:
            errors.append(f"Operator TTL migration is missing for {table}")

    return errors


def main() -> int:
    errors = collect_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("chart-data contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
