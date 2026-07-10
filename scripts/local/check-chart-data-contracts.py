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


def collect_errors() -> list[str]:
    errors: list[str] = []
    local_sql = read(LOCAL_DDL)
    k8s_sql = read(K8S_DDL)

    if normalized_market_ddl(local_sql) != normalized_market_ddl(k8s_sql):
        errors.append("ClickHouse init DDL copies differ outside declared headers/local-only agent table")

    tick_ttl = "TTL event_time + INTERVAL 21 DAY DELETE"
    for label, sql in (("local", local_sql), ("k8s", k8s_sql)):
        for table in ("trade_ticks", "quote_ticks"):
            if tick_ttl not in table_ddl(sql, table):
                errors.append(f"{label} {table} is missing the 21-day delete TTL")
        for table in ("chart_candles", "order_flow_profile_daily"):
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

    expected_processed = env_csv(ROOT / "docker-compose.yml", "KAFKA_PROCESSED_TOPICS")
    for path in (
        ROOT / "infra/k8s/base/app/configmap.yaml",
        ROOT / "infra/k8s/overlays/aws/configmap-aws-patch.yaml",
    ):
        if env_csv(path, "KAFKA_PROCESSED_TOPICS") != expected_processed:
            errors.append(f"Processed S3 topic contract differs: {path.relative_to(ROOT)}")

    terraform = read(ROOT / "infra/aws/terraform/main.tf")
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

    migration = read(ROOT / "scripts/local/migrate-chart-tick-retention.sql")
    for table in ("trade_ticks", "quote_ticks"):
        if f"ALTER TABLE market_data.{table}" not in migration or tick_ttl not in migration:
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
