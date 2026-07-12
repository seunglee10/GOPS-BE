"""Apply the PostgreSQL schema owned by the simplified chart geometry subsystem."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

from gops_agents.chart_assets.storage import _database_conninfo


SQL_PATH = Path(__file__).parent / "003_geometry_assets.sql"


def apply_schema(conninfo: str | None = None) -> None:
    with psycopg.connect(conninfo or _database_conninfo()) as conn:
        conn.execute(SQL_PATH.read_text(encoding="utf-8"))
        conn.commit()


def main() -> int:
    apply_schema()
    print(json.dumps({"schemaApplied": True, "contract": "geometry"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
