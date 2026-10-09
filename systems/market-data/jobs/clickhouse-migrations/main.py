"""Versioned ClickHouse migration runner for already-provisioned environments."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from market_data.storage.clickhouse_loader import ClickHouseHttpClient


MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def split_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def run_migrations(client: ClickHouseHttpClient) -> list[str]:
    client.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {client.database}.schema_migrations
        (
            filename String,
            checksum FixedString(64),
            transaction_mode LowCardinality(String) DEFAULT 'nontransactional',
            applied_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(applied_at)
        ORDER BY filename
        """
    )
    rows = client.query_json_each_row(
        f"""
        SELECT filename, argMax(checksum, applied_at) AS checksum
        FROM {client.database}.schema_migrations
        GROUP BY filename
        FORMAT JSONEachRow
        """
    )
    recorded = {str(row["filename"]): str(row["checksum"]) for row in rows}
    applied: list[str] = []
    for path in migration_files():
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if path.name in recorded:
            if recorded[path.name] != checksum:
                raise RuntimeError(f"applied ClickHouse migration changed: {path.name}")
            continue
        for statement in split_statements(sql):
            client.execute(statement)
        client.insert_json_each_row("schema_migrations", [{
            "filename": path.name,
            "checksum": checksum,
            "transaction_mode": "nontransactional",
            # schema_migrations.applied_at 은 DateTime64(3, 'UTC') 이고 JSONEachRow 파서가
            # isoformat() 의 '+00:00' 오프셋을 받지 못한다. 저장소 공통 포맷을 따른다.
            "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        }])
        applied.append(path.name)
    return applied


def main() -> int:
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    applied = run_migrations(client)
    print(f"ClickHouse migrations complete: applied={applied}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
