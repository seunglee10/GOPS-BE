from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


RISK_LEVELS = {"conservative", "balanced", "aggressive"}
HORIZONS = {"intraday"}
RUN_TERMINAL_STATUSES = {"completed", "empty", "market_closed", "profile_required", "failed"}


@dataclass(frozen=True)
class InvestmentProfileUpsert:
    user_sub: str
    risk_level: str
    horizon: str
    max_drawdown_pct: float
    preferred_sectors: list[str]
    excluded_sectors: list[str]
    excluded_symbols: list[str]


@dataclass(frozen=True)
class RecommendationRunCreate:
    user_sub: str
    run_key: str
    slot_start: str
    market_date: str
    status: str
    profile_snapshot: dict[str, Any]
    market_snapshot_time: str
    summary: dict[str, Any]


class RecommendationRepository:
    def get_profile(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_profile_user_subs(self) -> list[str]:
        raise NotImplementedError

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
        raise NotImplementedError

    def get_portfolio_snapshot(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def upsert_portfolio_snapshot(self, user_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def latest_run(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_run_by_key(self, user_sub: str, run_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def create_or_replace_run(self, run: RecommendationRunCreate, items: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError


class PostgresRecommendationRepository(RecommendationRepository):
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresRecommendationRepository":
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

    def get_profile(self, user_sub: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_investment_profiles WHERE user_sub = %s",
                (user_sub,),
            ).fetchone()
            return _json_ready(dict(row)) if row else None

    def list_profile_user_subs(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_sub FROM user_investment_profiles ORDER BY updated_at DESC, user_sub ASC",
            ).fetchall()
            return [str(row["user_sub"]) for row in rows if row.get("user_sub")]

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO user_investment_profiles (
                    user_sub, risk_level, horizon, max_drawdown_pct,
                    preferred_sectors, excluded_sectors, excluded_symbols, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (user_sub) DO UPDATE
                SET risk_level = EXCLUDED.risk_level,
                    horizon = EXCLUDED.horizon,
                    max_drawdown_pct = EXCLUDED.max_drawdown_pct,
                    preferred_sectors = EXCLUDED.preferred_sectors,
                    excluded_sectors = EXCLUDED.excluded_sectors,
                    excluded_symbols = EXCLUDED.excluded_symbols,
                    updated_at = now()
                RETURNING *
                """,
                (
                    profile.user_sub,
                    profile.risk_level,
                    profile.horizon,
                    profile.max_drawdown_pct,
                    Jsonb(profile.preferred_sectors),
                    Jsonb(profile.excluded_sectors),
                    Jsonb(profile.excluded_symbols),
                ),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row))

    def get_portfolio_snapshot(self, user_sub: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_portfolio_snapshots WHERE user_sub = %s",
                (user_sub,),
            ).fetchone()
            if not row:
                return None
            payload = dict(row)
            payload["payload"] = _json_ready(payload.get("payload") or {})
            return _json_ready(payload)

    def upsert_portfolio_snapshot(self, user_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO user_portfolio_snapshots (user_sub, payload, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_sub) DO UPDATE
                SET payload = EXCLUDED.payload,
                    updated_at = now()
                RETURNING *
                """,
                (user_sub, Jsonb(payload)),
            ).fetchone()
            conn.commit()
            return _json_ready(dict(row))

    def latest_run(self, user_sub: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM stock_recommendation_runs
                WHERE user_sub = %s
                ORDER BY generated_at DESC, id DESC
                LIMIT 1
                """,
                (user_sub,),
            ).fetchone()
            return self._run_with_items(conn, dict(row)) if row else None

    def get_run_by_key(self, user_sub: str, run_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stock_recommendation_runs WHERE user_sub = %s AND run_key = %s",
                (user_sub, run_key),
            ).fetchone()
            return self._run_with_items(conn, dict(row)) if row else None

    def create_or_replace_run(self, run: RecommendationRunCreate, items: list[dict[str, Any]]) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO stock_recommendation_runs (
                    user_sub, run_key, slot_start, market_date, status,
                    profile_snapshot, market_snapshot_time, summary, generated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (user_sub, run_key) DO UPDATE
                SET status = EXCLUDED.status,
                    profile_snapshot = EXCLUDED.profile_snapshot,
                    market_snapshot_time = EXCLUDED.market_snapshot_time,
                    summary = EXCLUDED.summary,
                    generated_at = now()
                RETURNING *
                """,
                (
                    run.user_sub,
                    run.run_key,
                    run.slot_start,
                    run.market_date,
                    run.status,
                    Jsonb(run.profile_snapshot),
                    run.market_snapshot_time,
                    Jsonb(run.summary),
                ),
            ).fetchone()
            run_id = int(row["id"])
            conn.execute("DELETE FROM stock_recommendation_items WHERE run_id = %s", (run_id,))
            for item in items:
                conn.execute(
                    """
                    INSERT INTO stock_recommendation_items (
                        run_id, symbol, action, rank, score, confidence, sector,
                        reasons, risk_warnings, metrics_snapshot
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        item["symbol"],
                        item.get("action", "buy"),
                        item["rank"],
                        item["score"],
                        item["confidence"],
                        item.get("sector"),
                        Jsonb(item.get("reasons") or []),
                        Jsonb(item.get("riskWarnings") or item.get("risk_warnings") or []),
                        Jsonb(item.get("metricsSnapshot") or item.get("metrics_snapshot") or {}),
                    ),
                )
            conn.commit()
            return self._run_with_items(conn, dict(row)) or _json_ready(dict(row))

    def _run_with_items(self, conn: psycopg.Connection, run: dict[str, Any]) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT * FROM stock_recommendation_items WHERE run_id = %s ORDER BY rank ASC, score DESC",
            (run["id"],),
        ).fetchall()
        payload = _json_ready(run)
        payload["items"] = [_json_ready(dict(row)) for row in rows]
        return payload

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)


class InMemoryRecommendationRepository(RecommendationRepository):
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {}
        self.runs: dict[int, dict[str, Any]] = {}
        self.items: dict[int, list[dict[str, Any]]] = {}
        self._run_id = 0

    def get_profile(self, user_sub: str) -> dict[str, Any] | None:
        row = self.profiles.get(user_sub)
        return _json_ready(deepcopy(row)) if row else None

    def list_profile_user_subs(self) -> list[str]:
        rows = sorted(self.profiles.values(), key=lambda item: (item["updated_at"], item["user_sub"]), reverse=True)
        return [str(row["user_sub"]) for row in rows]

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
        row = {
            "user_sub": profile.user_sub,
            "risk_level": profile.risk_level,
            "horizon": profile.horizon,
            "max_drawdown_pct": profile.max_drawdown_pct,
            "preferred_sectors": list(profile.preferred_sectors),
            "excluded_sectors": list(profile.excluded_sectors),
            "excluded_symbols": list(profile.excluded_symbols),
            "updated_at": datetime.now(timezone.utc),
        }
        self.profiles[profile.user_sub] = deepcopy(row)
        return _json_ready(row)

    def get_portfolio_snapshot(self, user_sub: str) -> dict[str, Any] | None:
        row = getattr(self, "portfolio_snapshots", {}).get(user_sub)
        return _json_ready(deepcopy(row)) if row else None

    def upsert_portfolio_snapshot(self, user_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self, "portfolio_snapshots"):
            self.portfolio_snapshots: dict[str, dict[str, Any]] = {}
        row = {"user_sub": user_sub, "payload": deepcopy(payload), "updated_at": datetime.now(timezone.utc)}
        self.portfolio_snapshots[user_sub] = deepcopy(row)
        return _json_ready(row)

    def latest_run(self, user_sub: str) -> dict[str, Any] | None:
        rows = [row for row in self.runs.values() if row["user_sub"] == user_sub]
        rows.sort(key=lambda item: (item["generated_at"], item["id"]), reverse=True)
        return self._with_items(rows[0]) if rows else None

    def get_run_by_key(self, user_sub: str, run_key: str) -> dict[str, Any] | None:
        for row in self.runs.values():
            if row["user_sub"] == user_sub and row["run_key"] == run_key:
                return self._with_items(row)
        return None

    def create_or_replace_run(self, run: RecommendationRunCreate, items: list[dict[str, Any]]) -> dict[str, Any]:
        existing_id = None
        for row_id, row in self.runs.items():
            if row["user_sub"] == run.user_sub and row["run_key"] == run.run_key:
                existing_id = row_id
                break
        run_id = existing_id
        if run_id is None:
            self._run_id += 1
            run_id = self._run_id
        row = {
            "id": run_id,
            "user_sub": run.user_sub,
            "run_key": run.run_key,
            "slot_start": run.slot_start,
            "market_date": run.market_date,
            "status": run.status,
            "profile_snapshot": deepcopy(run.profile_snapshot),
            "market_snapshot_time": run.market_snapshot_time,
            "summary": deepcopy(run.summary),
            "generated_at": datetime.now(timezone.utc),
        }
        self.runs[run_id] = row
        self.items[run_id] = [deepcopy(item) for item in items]
        return self._with_items(row)

    def _with_items(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = _json_ready(deepcopy(row))
        payload["items"] = _json_ready(deepcopy(sorted(self.items.get(row["id"], []), key=lambda item: item["rank"])))
        return payload


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value
