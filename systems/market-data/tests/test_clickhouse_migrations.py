from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "systems" / "market-data" / "jobs" / "clickhouse-migrations" / "main.py"
SPEC = importlib.util.spec_from_file_location("gops_clickhouse_migrations", MODULE_PATH)
assert SPEC and SPEC.loader
MIGRATIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATIONS)


class FakeClickHouseClient:
    database = "market_data"

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.records: dict[str, str] = {}

    def execute(self, sql: str) -> None:
        self.executed.append(sql.strip())

    def query_json_each_row(self, _sql: str) -> list[dict[str, str]]:
        return [
            {"filename": filename, "checksum": checksum}
            for filename, checksum in sorted(self.records.items())
        ]

    def insert_json_each_row(self, table: str, rows: list[dict[str, str]]) -> None:
        assert table == "schema_migrations"
        for row in rows:
            self.records[row["filename"]] = row["checksum"]


def test_clickhouse_migrations_are_versioned_idempotent_and_checksum_guarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = tmp_path / "0001_test.sql"
    migration.write_text("CREATE TABLE one (id UInt64); CREATE VIEW one_view AS SELECT * FROM one;", encoding="utf-8")
    monkeypatch.setattr(MIGRATIONS, "MIGRATIONS_DIR", tmp_path)
    client = FakeClickHouseClient()

    assert MIGRATIONS.run_migrations(client) == ["0001_test.sql"]
    statement_count = len(client.executed)
    assert MIGRATIONS.run_migrations(client) == []
    assert len(client.executed) == statement_count + 1  # state-table bootstrap only

    migration.write_text("CREATE TABLE changed (id UInt64);", encoding="utf-8")
    with pytest.raises(RuntimeError, match="applied ClickHouse migration changed"):
        MIGRATIONS.run_migrations(client)


def test_market_identity_migration_defines_stable_latest_views() -> None:
    sql = (MODULE_PATH.parent / "migrations" / "0001_instrument_identity_and_latest_views.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS instrument_id Nullable(UUID)" in sql
    assert "CREATE OR REPLACE VIEW market_data.symbols_latest" in sql
    assert "CREATE OR REPLACE VIEW market_data.chart_candles_latest" in sql
    assert "CREATE OR REPLACE VIEW market_data.company_journal_reports" in sql
    assert " FINAL" not in sql.upper()

    init_sql = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "infra" / "clickhouse" / "initdb").glob("*.sql"))
    )
    replacing_tables: list[str] = []
    for statement in re.split(r";\s*", init_sql):
        table = re.search(r"CREATE TABLE IF NOT EXISTS market_data\.(\w+)", statement, re.IGNORECASE)
        if table and re.search(r"ENGINE\s*=\s*ReplacingMergeTree", statement, re.IGNORECASE):
            replacing_tables.append(table.group(1))
    view_names = set(re.findall(r"CREATE OR REPLACE VIEW market_data\.(\w+)", sql, re.IGNORECASE))
    assert [table for table in replacing_tables if f"{table}_latest" not in view_names] == []
