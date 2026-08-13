from __future__ import annotations

from pathlib import Path

from market_data.storage.clickhouse_loader import ClickHouseHttpClient

from .repository import CompanyJournalRepository


def main() -> int:
    repository = CompanyJournalRepository()
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    statements = [statement.strip() for statement in schema.split(";") if statement.strip()]
    for statement in statements:
        repository.client.execute(statement)
    print(f"company journal ClickHouse migration complete: statements={len(statements)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
