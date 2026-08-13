#!/usr/bin/env python3
"""Compare the ERDCloud PostgreSQL projection with a migrated database."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import psycopg


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ERD = ROOT / "docs" / "ERDCLOUD_IMPORT.sql"
CREATE_TABLE_RE = re.compile(r"CREATE TABLE\s+([`\w]+)\s*\((.*?)\);", re.I | re.S)
ALTER_ADD_COLUMN_RE = re.compile(
    r"ALTER TABLE\s+([`\w]+)\s+ADD COLUMN\s+([`\w]+)\s+[^;]+;",
    re.I,
)
ALTER_FK_RE = re.compile(
    r"ALTER TABLE\s+([`\w]+).*?FOREIGN KEY\s*\(([^)]+)\)\s*REFERENCES\s+([`\w]+)\s*\(([^)]+)\)",
    re.I | re.S,
)
INLINE_FK_RE = re.compile(
    r"FOREIGN KEY\s*\(([^)]+)\)\s*REFERENCES\s+([`\w]+)\s*\(([^)]+)\)",
    re.I | re.S,
)


def _clean(value: str) -> str:
    return value.strip().strip("`").lower()


def _split_columns(value: str) -> tuple[str, ...]:
    return tuple(_clean(item) for item in value.split(","))


def parse_erd(path: Path) -> dict[str, dict[str, Any]]:
    sql = path.read_text(encoding="utf-8")
    postgres_sql = sql.split("-- ClickHouse logical model:", maxsplit=1)[0]
    schema: dict[str, dict[str, Any]] = {}
    for match in CREATE_TABLE_RE.finditer(postgres_sql):
        name = _clean(match.group(1))
        body = match.group(2)
        columns: set[str] = set()
        for line in body.splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or re.match(r"(PRIMARY|UNIQUE|CONSTRAINT|FOREIGN|REFERENCES|CHECK|KEY)\b", stripped, re.I):
                continue
            column_match = re.match(r"([`\w]+)\s+", stripped)
            if column_match:
                columns.add(_clean(column_match.group(1)))
        pk_match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", body, re.I | re.S)
        fks = {
            (_split_columns(local), _clean(target), _split_columns(remote))
            for local, target, remote in INLINE_FK_RE.findall(body)
        }
        schema[name] = {
            "columns": columns,
            "pk": _split_columns(pk_match.group(1)) if pk_match else (),
            "fks": fks,
        }
    for table, column in ALTER_ADD_COLUMN_RE.findall(postgres_sql):
        table_name = _clean(table)
        if table_name in schema:
            schema[table_name]["columns"].add(_clean(column))
    for table, local, target, remote in ALTER_FK_RE.findall(postgres_sql):
        table_name = _clean(table)
        if table_name in schema:
            schema[table_name]["fks"].add((_split_columns(local), _clean(target), _split_columns(remote)))
    return schema


def read_database_schema(conninfo: str) -> dict[str, dict[str, Any]]:
    schema: dict[str, dict[str, Any]] = {}
    with psycopg.connect(conninfo) as conn:
        columns = conn.execute(
            """
            SELECT table_schema, table_name, column_name
            FROM information_schema.columns
            WHERE table_schema IN ('public', 'chart_assets')
            ORDER BY table_schema, table_name, ordinal_position
            """
        ).fetchall()
        for table_schema, table_name, column_name in columns:
            display_name = table_name if table_schema == "public" else f"chart_assets_{table_name}"
            if display_name == "schema_migrations":
                continue
            schema.setdefault(display_name, {"columns": set(), "pk": (), "fks": set()})["columns"].add(column_name)

        primary_keys = conn.execute(
            """
            SELECT ns.nspname, rel.relname,
                   array_agg(att.attname ORDER BY key_column.ordinality)
            FROM pg_constraint AS con
            JOIN pg_class AS rel ON rel.oid = con.conrelid
            JOIN pg_namespace AS ns ON ns.oid = rel.relnamespace
            JOIN unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ordinality) ON true
            JOIN pg_attribute AS att ON att.attrelid = rel.oid AND att.attnum = key_column.attnum
            WHERE con.contype = 'p' AND ns.nspname IN ('public', 'chart_assets')
            GROUP BY ns.nspname, rel.relname
            """
        ).fetchall()
        for table_schema, table_name, columns_value in primary_keys:
            display_name = table_name if table_schema == "public" else f"chart_assets_{table_name}"
            if display_name in schema:
                schema[display_name]["pk"] = tuple(columns_value)

        foreign_keys = conn.execute(
            """
            SELECT con.oid, source_ns.nspname, source.relname,
                   array_agg(source_att.attname ORDER BY key_column.ordinality),
                   target_ns.nspname, target.relname,
                   array_agg(target_att.attname ORDER BY key_column.ordinality)
            FROM pg_constraint AS con
            JOIN pg_class AS source ON source.oid = con.conrelid
            JOIN pg_namespace AS source_ns ON source_ns.oid = source.relnamespace
            JOIN pg_class AS target ON target.oid = con.confrelid
            JOIN pg_namespace AS target_ns ON target_ns.oid = target.relnamespace
            JOIN unnest(con.conkey, con.confkey) WITH ORDINALITY
                 AS key_column(source_attnum, target_attnum, ordinality) ON true
            JOIN pg_attribute AS source_att
              ON source_att.attrelid = source.oid AND source_att.attnum = key_column.source_attnum
            JOIN pg_attribute AS target_att
              ON target_att.attrelid = target.oid AND target_att.attnum = key_column.target_attnum
            WHERE con.contype = 'f' AND source_ns.nspname IN ('public', 'chart_assets')
            GROUP BY con.oid, source_ns.nspname, source.relname, target_ns.nspname, target.relname
            """
        ).fetchall()
        for _constraint_oid, source_ns, source_table, local, target_ns, target_table, remote in foreign_keys:
            source_name = source_table if source_ns == "public" else f"chart_assets_{source_table}"
            target_name = target_table if target_ns == "public" else f"chart_assets_{target_table}"
            if source_name in schema:
                schema[source_name]["fks"].add((tuple(local), target_name, tuple(remote)))
    return schema


def compare(expected: dict[str, dict[str, Any]], actual: dict[str, dict[str, Any]]) -> dict[str, Any]:
    expected_tables = set(expected)
    actual_tables = set(actual)
    differences: dict[str, Any] = {
        "missing_tables": sorted(expected_tables - actual_tables),
        "extra_tables": sorted(actual_tables - expected_tables),
        "tables": {},
    }
    for table in sorted(expected_tables & actual_tables):
        table_diff = {
            "missing_columns": sorted(expected[table]["columns"] - actual[table]["columns"]),
            "extra_columns": sorted(actual[table]["columns"] - expected[table]["columns"]),
            "expected_pk": expected[table]["pk"],
            "actual_pk": actual[table]["pk"],
            "missing_fks": sorted(expected[table]["fks"] - actual[table]["fks"]),
            "extra_fks": sorted(actual[table]["fks"] - expected[table]["fks"]),
        }
        if (
            table_diff["missing_columns"] or table_diff["extra_columns"] or
            table_diff["expected_pk"] != table_diff["actual_pk"] or
            table_diff["missing_fks"] or table_diff["extra_fks"]
        ):
            differences["tables"][table] = table_diff
    return differences


def has_drift(differences: dict[str, Any]) -> bool:
    return bool(differences["missing_tables"] or differences["extra_tables"] or differences["tables"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--erd", type=Path, default=DEFAULT_ERD)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    differences = compare(parse_erd(args.erd), read_database_schema(args.database_url))
    print(json.dumps(differences, ensure_ascii=False, indent=2, sort_keys=True, default=list))
    return 1 if args.strict and has_drift(differences) else 0


if __name__ == "__main__":
    raise SystemExit(main())
