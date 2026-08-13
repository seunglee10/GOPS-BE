"""Apply the PostgreSQL schema owned by the simplified chart geometry subsystem."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

SQL_PATHS = (
    Path(__file__).parent / "001_create_chart_assets.sql",
    Path(__file__).parent / "002_expand_chart_asset_intervals.sql",
    Path(__file__).parent / "003_geometry_assets.sql",
    Path(__file__).parent / "004_chart_asset_queue_priority.sql",
    Path(__file__).parent / "005_geometry_asset_simulation_snapshots.sql",
)


def _database_conninfo() -> str:
    if conninfo := os.getenv("DATABASE_URL"):
        return conninfo
    return make_conninfo(
        host=os.environ["DATABASE_HOST"],
        port=os.getenv("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
    )


def apply_schema(conninfo: str | None = None) -> None:
    with psycopg.connect(conninfo or _database_conninfo()) as conn:
        for sql_path in SQL_PATHS:
            conn.execute(sql_path.read_text(encoding="utf-8"))
        conn.commit()


def main() -> int:
    apply_schema()
    print(json.dumps({"schemaApplied": True, "contract": "geometry"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
