"""Explicit SQL migration runner."""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
NONTRANSACTIONAL_DIRECTIVE = "-- migration: nontransactional"


class MigrationChecksumMismatch(RuntimeError):
    """Raised when an already-applied migration file has been modified."""


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _migration_metadata(path: Path) -> tuple[str, str, str]:
    sql = path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    transaction_mode = "nontransactional" if sql.lstrip().startswith(NONTRANSACTIONAL_DIRECTIVE) else "transactional"
    return sql, checksum, transaction_mode


def _nontransactional_statements(sql: str) -> list[str]:
    """Split deliberately simple non-transactional migration files.

    Non-transactional files are reserved for standalone DDL such as CREATE
    INDEX CONCURRENTLY. Function bodies and quoted semicolons belong in normal
    transactional migrations.
    """

    statements: list[str] = []
    for chunk in sql.split(";"):
        statement = "\n".join(
            line for line in chunk.splitlines() if line.strip() != NONTRANSACTIONAL_DIRECTIVE
        ).strip()
        if statement:
            statements.append(statement)
    return statements


def run_migrations(conninfo: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                checksum TEXT,
                transaction_mode TEXT NOT NULL DEFAULT 'transactional',
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
        conn.execute(
            "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS transaction_mode TEXT NOT NULL DEFAULT 'transactional'"
        )
        conn.commit()

        for path in migration_files():
            sql, checksum, transaction_mode = _migration_metadata(path)
            existing = conn.execute(
                "SELECT checksum, transaction_mode FROM schema_migrations WHERE filename = %s",
                (path.name,),
            ).fetchone()
            if existing:
                recorded_checksum, recorded_mode = existing
                if recorded_checksum is None:
                    conn.execute(
                        "UPDATE schema_migrations SET checksum = %s, transaction_mode = %s WHERE filename = %s",
                        (checksum, transaction_mode, path.name),
                    )
                    conn.commit()
                elif recorded_checksum != checksum or recorded_mode != transaction_mode:
                    raise MigrationChecksumMismatch(
                        f"applied migration changed: {path.name} "
                        f"(expected {recorded_checksum}/{recorded_mode}, got {checksum}/{transaction_mode})"
                    )
                continue

            if transaction_mode == "nontransactional":
                conn.commit()
                conn.autocommit = True
                try:
                    for statement in _nontransactional_statements(sql):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum, transaction_mode) VALUES (%s, %s, %s)",
                        (path.name, checksum, transaction_mode),
                    )
                finally:
                    conn.autocommit = False
            else:
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum, transaction_mode) VALUES (%s, %s, %s)",
                        (path.name, checksum, transaction_mode),
                    )
            applied.append(path.name)
        conn.commit()
    return applied


def reset_public_schema(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
