"""Per-user layout preset storage backed by PostgreSQL.

One row per user in ``user_layout_presets`` (``user_sub`` primary key) holds the whole
preset list as JSONB. Mirrors the recommendations repository pattern (psycopg 3, one
connection per operation). An in-memory variant is used for auth-disabled/local runs and
tests.
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from kis_trader.persistence.user_context import apply_postgres_user_context


class ChartPresetsRepository:
    def read_presets(self, user_sub: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def replace_presets(self, user_sub: str, presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError


def _normalize(presets: Any) -> list[dict[str, Any]]:
    if not isinstance(presets, list):
        return []
    return [preset for preset in presets if isinstance(preset, dict)]


class PostgresChartPresetsRepository(ChartPresetsRepository):
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresChartPresetsRepository":
        conninfo = os.getenv("DATABASE_URL")
        if not conninfo:
            conninfo = make_conninfo(
                host=os.environ["DATABASE_HOST"],
                port=os.getenv("DATABASE_PORT", "5432"),
                dbname=os.environ["DATABASE_NAME"],
                user=os.environ["DATABASE_USER"],
                password=os.environ["DATABASE_PASSWORD"],
            )
        return cls(conninfo)

    def read_presets(self, user_sub: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT presets FROM user_layout_presets WHERE user_sub = %s",
                (user_sub,),
            ).fetchone()
            return _normalize(row.get("presets") if row else None)

    def replace_presets(self, user_sub: str, presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = _normalize(presets)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO user_layout_presets (user_sub, presets, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_sub) DO UPDATE
                SET presets = EXCLUDED.presets,
                    updated_at = now()
                RETURNING presets
                """,
                (user_sub, Jsonb(normalized)),
            ).fetchone()
            conn.commit()
            return _normalize(row.get("presets") if row else normalized)

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.conninfo, row_factory=dict_row)
        apply_postgres_user_context(conn)
        return conn


class InMemoryChartPresetsRepository(ChartPresetsRepository):
    def __init__(self) -> None:
        self.presets: dict[str, list[dict[str, Any]]] = {}

    def read_presets(self, user_sub: str) -> list[dict[str, Any]]:
        return deepcopy(self.presets.get(user_sub, []))

    def replace_presets(self, user_sub: str, presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = _normalize(presets)
        self.presets[user_sub] = deepcopy(normalized)
        return deepcopy(normalized)
