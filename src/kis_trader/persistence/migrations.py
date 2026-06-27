"""Explicit SQL migration runner."""

from __future__ import annotations

from pathlib import Path

import psycopg


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def run_migrations(conninfo: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for path in migration_files():
            existing = conn.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (path.name,)).fetchone()
            if existing:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)
        conn.commit()
    return applied


def reset_public_schema(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
