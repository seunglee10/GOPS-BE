from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.errors import UndefinedTable, UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


RISK_LEVELS = {"conservative", "balanced", "aggressive"}
RECOMMENDATION_STYLES = {"momentum", "balanced", "stable"}
HORIZONS = {"intraday"}
RUN_TERMINAL_STATUSES = {"completed", "empty", "market_closed", "profile_required", "failed"}
class RecommendationSchemaUnavailable(RuntimeError):
    """Raised when recommendation tables have not been migrated yet."""


@dataclass(frozen=True)
class InvestmentProfileUpsert:
    user_sub: str
    risk_level: str
    recommendation_style: str
    horizon: str
    max_drawdown_pct: float
    preferred_sectors: list[str]
    excluded_sectors: list[str]
    excluded_symbols: list[str]


@dataclass(frozen=True)
class ScoreProfileUpsert:
    user_sub: str
    name: str
    schema_version: str
    block_weights: dict[str, float]
    factor_weights: dict[str, dict[str, float]]
    portfolio_weight: float
    portfolio_factor_weights: dict[str, float]


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
    portfolio_snapshot_history_id: int | None = None
    weights_version: str = "legacy"
    algorithm_version: str = "legacy"
    fundamental_snapshot_provenance: dict[str, Any] | None = None
    evidence_snapshot_id: int | None = None
    scoring_input_digest: str | None = None
    scoring_snapshot: dict[str, Any] | None = None


class RecommendationRepository:
    def get_profile(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_profile_at(self, user_sub: str, cutoff: datetime) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_profile_user_subs(self) -> list[str]:
        raise NotImplementedError

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
        raise NotImplementedError

    def list_score_profiles(self, user_sub: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def create_score_profile(self, profile: ScoreProfileUpsert, *, max_profiles: int) -> dict[str, Any]:
        raise NotImplementedError

    def update_score_profile(self, profile_id: int, profile: ScoreProfileUpsert) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete_score_profile(self, user_sub: str, profile_id: int) -> bool:
        raise NotImplementedError

    def activate_score_profile(self, user_sub: str, profile_id: int | None, *, preset_style: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_portfolio_snapshot(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_portfolio_snapshot_at(self, user_sub: str, cutoff: datetime) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_active_weight_set(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def upsert_portfolio_snapshot(self, user_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def list_daily_portfolio_snapshots(self, user_sub: str, start_at: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def latest_run(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def latest_run_for_session(self, user_sub: str, session_mode: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_run_by_key(self, user_sub: str, run_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def create_or_replace_run(self, run: RecommendationRunCreate, items: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError

    def get_evidence_snapshot(self, snapshot_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_evidence_snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def create_evidence_snapshot(
        self, snapshot: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
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
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM user_investment_profiles WHERE user_sub = %s",
                    (user_sub,),
                ).fetchone()
                return _json_ready(dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_profile_at(self, user_sub: str, cutoff: datetime) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, user_sub, payload, source_as_of, created_at
                    FROM user_investment_profile_history
                    WHERE user_sub = %s AND source_as_of <= %s
                    ORDER BY source_as_of DESC, id DESC
                    LIMIT 1
                    """,
                    (user_sub, cutoff),
                ).fetchone()
                if not row:
                    return None
                payload = _json_ready(row.get("payload") or {})
                return {
                    **payload,
                    "history_id": int(row["id"]),
                    "source_as_of": _json_ready(row["source_as_of"]),
                }
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def list_profile_user_subs(self) -> list[str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT user_sub FROM user_investment_profiles ORDER BY updated_at DESC, user_sub ASC",
                ).fetchall()
                return [str(row["user_sub"]) for row in rows if row.get("user_sub")]
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO user_investment_profiles (
                        user_sub, risk_level, recommendation_style, horizon, max_drawdown_pct,
                        preferred_sectors, excluded_sectors, excluded_symbols, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (user_sub) DO UPDATE
                    SET risk_level = EXCLUDED.risk_level,
                        horizon = EXCLUDED.horizon,
                        recommendation_style = EXCLUDED.recommendation_style,
                        max_drawdown_pct = EXCLUDED.max_drawdown_pct,
                        preferred_sectors = EXCLUDED.preferred_sectors,
                        excluded_sectors = EXCLUDED.excluded_sectors,
                        excluded_symbols = EXCLUDED.excluded_symbols,
                        profile_revision = user_investment_profiles.profile_revision + CASE WHEN ROW(
                            user_investment_profiles.risk_level,
                            user_investment_profiles.recommendation_style,
                            user_investment_profiles.horizon,
                            user_investment_profiles.max_drawdown_pct,
                            user_investment_profiles.preferred_sectors,
                            user_investment_profiles.excluded_sectors,
                            user_investment_profiles.excluded_symbols
                        ) IS DISTINCT FROM ROW(
                            EXCLUDED.risk_level,
                            EXCLUDED.recommendation_style,
                            EXCLUDED.horizon,
                            EXCLUDED.max_drawdown_pct,
                            EXCLUDED.preferred_sectors,
                            EXCLUDED.excluded_sectors,
                            EXCLUDED.excluded_symbols
                        ) THEN 1 ELSE 0 END,
                        updated_at = CASE WHEN ROW(
                            user_investment_profiles.risk_level,
                            user_investment_profiles.recommendation_style,
                            user_investment_profiles.horizon,
                            user_investment_profiles.max_drawdown_pct,
                            user_investment_profiles.preferred_sectors,
                            user_investment_profiles.excluded_sectors,
                            user_investment_profiles.excluded_symbols
                        ) IS DISTINCT FROM ROW(
                            EXCLUDED.risk_level,
                            EXCLUDED.recommendation_style,
                            EXCLUDED.horizon,
                            EXCLUDED.max_drawdown_pct,
                            EXCLUDED.preferred_sectors,
                            EXCLUDED.excluded_sectors,
                            EXCLUDED.excluded_symbols
                        ) THEN now() ELSE user_investment_profiles.updated_at END
                    RETURNING *
                    """,
                    (
                        profile.user_sub,
                        profile.risk_level,
                        profile.recommendation_style,
                        profile.horizon,
                        profile.max_drawdown_pct,
                        Jsonb(profile.preferred_sectors),
                        Jsonb(profile.excluded_sectors),
                        Jsonb(profile.excluded_symbols),
                    ),
                ).fetchone()
                profile_payload = {
                    "risk_level": row["risk_level"],
                    "recommendation_style": row.get("recommendation_style") or "balanced",
                    "horizon": row["horizon"],
                    "max_drawdown_pct": float(row["max_drawdown_pct"]),
                    "preferred_sectors": _json_ready(row["preferred_sectors"]),
                    "excluded_sectors": _json_ready(row["excluded_sectors"]),
                    "excluded_symbols": _json_ready(row["excluded_symbols"]),
                }
                conn.execute(
                    """
                    INSERT INTO user_investment_profile_history (user_sub, payload, source_as_of)
                    SELECT %s, %s, %s
                    WHERE %s IS DISTINCT FROM (
                        SELECT payload
                        FROM user_investment_profile_history
                        WHERE user_sub = %s
                        ORDER BY source_as_of DESC, id DESC
                        LIMIT 1
                    )
                    """,
                    (
                        profile.user_sub,
                        Jsonb(profile_payload),
                        row["updated_at"],
                        Jsonb(profile_payload),
                        profile.user_sub,
                    ),
                )
                conn.commit()
                return _json_ready(dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def list_score_profiles(self, user_sub: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM user_recommendation_score_profiles
                    WHERE user_sub = %s
                    ORDER BY lower(name), id
                    """,
                    (user_sub,),
                ).fetchall()
                return [_json_ready(dict(row)) for row in rows]
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def create_score_profile(self, profile: ScoreProfileUpsert, *, max_profiles: int) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                conn.execute(
                    "SELECT user_sub FROM user_investment_profiles WHERE user_sub = %s FOR UPDATE",
                    (profile.user_sub,),
                ).fetchone()
                count = conn.execute(
                    "SELECT count(*) AS count FROM user_recommendation_score_profiles WHERE user_sub = %s",
                    (profile.user_sub,),
                ).fetchone()
                if int(count["count"] or 0) >= max_profiles:
                    raise ValueError("score profile limit reached")
                row = conn.execute(
                    """
                    INSERT INTO user_recommendation_score_profiles (
                        user_sub, name, schema_version, block_weights, factor_weights,
                        portfolio_weight, portfolio_factor_weights
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        profile.user_sub, profile.name, profile.schema_version,
                        Jsonb(profile.block_weights), Jsonb(profile.factor_weights),
                        profile.portfolio_weight, Jsonb(profile.portfolio_factor_weights),
                    ),
                ).fetchone()
                conn.commit()
                return _json_ready(dict(row))
        except UniqueViolation as exc:
            raise ValueError("score profile name already exists") from exc
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def update_score_profile(self, profile_id: int, profile: ScoreProfileUpsert) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM user_recommendation_score_profiles WHERE id = %s AND user_sub = %s FOR UPDATE",
                    (profile_id, profile.user_sub),
                ).fetchone()
                if not existing:
                    return None
                existing_payload = _json_ready(dict(existing))
                requested_payload = {
                    "name": profile.name,
                    "schema_version": profile.schema_version,
                    "block_weights": profile.block_weights,
                    "factor_weights": profile.factor_weights,
                    "portfolio_weight": float(profile.portfolio_weight),
                    "portfolio_factor_weights": profile.portfolio_factor_weights,
                }
                if all(existing_payload.get(key) == value for key, value in requested_payload.items()):
                    return existing_payload
                row = conn.execute(
                    """
                    UPDATE user_recommendation_score_profiles
                    SET name = %s, schema_version = %s, block_weights = %s,
                        factor_weights = %s, portfolio_weight = %s,
                        portfolio_factor_weights = %s, revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_sub = %s
                    RETURNING *
                    """,
                    (
                        profile.name, profile.schema_version, Jsonb(profile.block_weights),
                        Jsonb(profile.factor_weights), profile.portfolio_weight,
                        Jsonb(profile.portfolio_factor_weights), profile_id, profile.user_sub,
                    ),
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        UPDATE user_investment_profiles
                        SET profile_revision = profile_revision + 1, updated_at = now()
                        WHERE user_sub = %s AND active_score_profile_id = %s
                        """,
                        (profile.user_sub, profile_id),
                    )
                conn.commit()
                return _json_ready(dict(row)) if row else None
        except UniqueViolation as exc:
            raise ValueError("score profile name already exists") from exc
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def delete_score_profile(self, user_sub: str, profile_id: int) -> bool:
        try:
            with self._connect() as conn:
                active = conn.execute(
                    "SELECT active_score_profile_id FROM user_investment_profiles WHERE user_sub = %s FOR UPDATE",
                    (user_sub,),
                ).fetchone()
                if active and active.get("active_score_profile_id") == profile_id:
                    conn.execute(
                        """
                        UPDATE user_investment_profiles
                        SET active_score_profile_id = NULL, recommendation_style = 'balanced',
                            profile_revision = profile_revision + 1, updated_at = now()
                        WHERE user_sub = %s
                        """,
                        (user_sub,),
                    )
                deleted = conn.execute(
                    "DELETE FROM user_recommendation_score_profiles WHERE id = %s AND user_sub = %s RETURNING id",
                    (profile_id, user_sub),
                ).fetchone()
                conn.commit()
                return bool(deleted)
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def activate_score_profile(self, user_sub: str, profile_id: int | None, *, preset_style: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                if profile_id is not None:
                    owned = conn.execute(
                        "SELECT id FROM user_recommendation_score_profiles WHERE id = %s AND user_sub = %s",
                        (profile_id, user_sub),
                    ).fetchone()
                    if not owned:
                        return None
                current = conn.execute(
                    "SELECT * FROM user_investment_profiles WHERE user_sub = %s FOR UPDATE",
                    (user_sub,),
                ).fetchone()
                if not current:
                    return None
                if current.get("active_score_profile_id") == profile_id and str(current.get("recommendation_style")) == preset_style:
                    return _json_ready(dict(current))
                row = conn.execute(
                    """
                    UPDATE user_investment_profiles
                    SET active_score_profile_id = %s, recommendation_style = %s,
                        profile_revision = profile_revision + 1, updated_at = now()
                    WHERE user_sub = %s
                    RETURNING *
                    """,
                    (profile_id, preset_style, user_sub),
                ).fetchone()
                conn.commit()
                return _json_ready(dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_portfolio_snapshot(self, user_sub: str) -> dict[str, Any] | None:
        try:
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
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_portfolio_snapshot_at(self, user_sub: str, cutoff: datetime) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, user_sub, payload, source_as_of, created_at
                    FROM user_portfolio_snapshot_history
                    WHERE user_sub = %s AND source_as_of <= %s
                    ORDER BY source_as_of DESC, id DESC
                    LIMIT 1
                    """,
                    (user_sub, cutoff),
                ).fetchone()
                return _json_ready(dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_active_weight_set(self) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT model_version, training_cutoff, weights, validation_report
                    FROM stock_recommendation_model_registry
                    WHERE status = 'active'
                    ORDER BY activated_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                if not row:
                    return None
                return {
                    "version": row["model_version"],
                    "trainingCutoff": _json_ready(row["training_cutoff"]),
                    "styles": _json_ready(row["weights"]),
                    "validation": _json_ready(row["validation_report"]),
                }
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_evidence_snapshot(self, snapshot_key: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM stock_recommendation_evidence_snapshots WHERE snapshot_key = %s",
                    (snapshot_key,),
                ).fetchone()
                return self._evidence_snapshot_with_candidates(conn, dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_evidence_snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM stock_recommendation_evidence_snapshots WHERE id = %s",
                    (snapshot_id,),
                ).fetchone()
                return self._evidence_snapshot_with_candidates(conn, dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def create_evidence_snapshot(
        self, snapshot: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with conn.transaction():
                    row = conn.execute(
                        """
                        INSERT INTO stock_recommendation_evidence_snapshots (
                            snapshot_key, slot_start, market_date, session_mode, cutoff, universe,
                            rule_set_version, source_digests, source_status, status, input_digest
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (snapshot_key) DO NOTHING
                        RETURNING *
                        """,
                        (
                            snapshot["snapshotKey"], snapshot["slotStart"], snapshot["marketDate"],
                            snapshot["sessionMode"], snapshot["cutoff"], Jsonb(snapshot["universe"]),
                            snapshot["ruleSetVersion"], Jsonb(snapshot["sourceDigests"]),
                            Jsonb(snapshot.get("sourceStatus") or {}), snapshot["status"], snapshot["inputDigest"],
                        ),
                    ).fetchone()
                    if row is None:
                        row = conn.execute(
                            "SELECT * FROM stock_recommendation_evidence_snapshots WHERE snapshot_key = %s",
                            (snapshot["snapshotKey"],),
                        ).fetchone()
                        return self._evidence_snapshot_with_candidates(conn, dict(row))
                    snapshot_id = int(row["id"])
                    for candidate in candidates:
                        conn.execute(
                            """
                            INSERT INTO stock_recommendation_evidence_candidates (
                                snapshot_id, symbol, sector, industry, change_percent, raw_factors,
                                normalized_factors, block_scores, base_setup_score, evidence_reliability,
                                reliability_components, rejection_reasons, daily_returns_60, market_item,
                                narrative_context, input_digest
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                snapshot_id, candidate["symbol"], candidate["sector"], candidate["industry"],
                                candidate.get("changePercent"), Jsonb(candidate.get("rawFactors") or {}),
                                Jsonb(candidate.get("normalizedFactors") or {}), Jsonb(candidate.get("blockScores") or {}),
                                candidate["baseSetupScore"], candidate["evidenceReliability"],
                                Jsonb(candidate.get("reliabilityComponents") or {}),
                                Jsonb(candidate.get("rejectionReasons") or []), Jsonb(candidate.get("dailyReturns60") or []),
                                Jsonb(candidate.get("marketItem") or {}), Jsonb(candidate.get("narrativeContext") or {}),
                                candidate["inputDigest"],
                            ),
                        )
                    return self._evidence_snapshot_with_candidates(conn, dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def upsert_portfolio_snapshot(self, user_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                source_as_of = payload.get("asOf") or payload.get("sourceAsOf")
                # The normalizer emits a fresh asOf on every poll. Serialize writers per
                # account so observation timestamps can update the latest row without
                # creating duplicate point-in-time history for the same portfolio state.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (user_sub,),
                )
                row = conn.execute(
                    """
                    WITH previous AS (
                        SELECT payload
                        FROM user_portfolio_snapshots
                        WHERE user_sub = %s
                    ), upserted AS (
                        INSERT INTO user_portfolio_snapshots (user_sub, payload, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (user_sub) DO UPDATE
                        SET payload = EXCLUDED.payload,
                            updated_at = now()
                        RETURNING user_sub, payload, updated_at
                    ), history AS (
                        INSERT INTO user_portfolio_snapshot_history (user_sub, payload, source_as_of)
                        SELECT upserted.user_sub, upserted.payload, COALESCE(%s::timestamptz, now())
                        FROM upserted
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM previous
                            WHERE (previous.payload - 'asOf' - 'sourceAsOf')
                                IS NOT DISTINCT FROM (upserted.payload - 'asOf' - 'sourceAsOf')
                        )
                    )
                    SELECT user_sub, payload, updated_at
                    FROM upserted
                    """,
                    (user_sub, user_sub, Jsonb(payload), source_as_of),
                ).fetchone()
                conn.commit()
                return _json_ready(dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def list_daily_portfolio_snapshots(self, user_sub: str, start_at: str | None = None) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT payload, source_as_of
                    FROM (
                        SELECT DISTINCT ON ((source_as_of AT TIME ZONE 'UTC')::date)
                            payload,
                            source_as_of
                        FROM user_portfolio_snapshot_history
                        WHERE user_sub = %s
                          AND (%s::timestamptz IS NULL OR source_as_of >= %s::timestamptz)
                        ORDER BY (source_as_of AT TIME ZONE 'UTC')::date, source_as_of DESC
                    ) AS daily
                    ORDER BY source_as_of ASC
                    """,
                    (user_sub, start_at, start_at),
                ).fetchall()
                return [_json_ready(dict(row)) for row in rows]
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def latest_run(self, user_sub: str) -> dict[str, Any] | None:
        try:
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
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def latest_run_for_session(self, user_sub: str, session_mode: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM stock_recommendation_runs
                    WHERE user_sub = %s
                      AND COALESCE(summary->>'sessionMode', 'regular') = %s
                    ORDER BY generated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (user_sub, session_mode),
                ).fetchone()
                return self._run_with_items(conn, dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def get_run_by_key(self, user_sub: str, run_key: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM stock_recommendation_runs WHERE user_sub = %s AND run_key = %s",
                    (user_sub, run_key),
                ).fetchone()
                return self._run_with_items(conn, dict(row)) if row else None
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def create_or_replace_run(self, run: RecommendationRunCreate, items: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO stock_recommendation_runs (
                        user_sub, run_key, slot_start, market_date, status,
                        profile_snapshot, market_snapshot_time, summary,
                        portfolio_snapshot_history_id, weights_version,
                        scoring_input_digest, scoring_snapshot,
                        algorithm_version, fundamental_snapshot_provenance,
                        evidence_snapshot_id, generated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (user_sub, run_key) DO UPDATE
                    SET status = EXCLUDED.status,
                        profile_snapshot = EXCLUDED.profile_snapshot,
                        market_snapshot_time = EXCLUDED.market_snapshot_time,
                        summary = EXCLUDED.summary,
                        portfolio_snapshot_history_id = EXCLUDED.portfolio_snapshot_history_id,
                        weights_version = EXCLUDED.weights_version,
                        scoring_input_digest = EXCLUDED.scoring_input_digest,
                        scoring_snapshot = EXCLUDED.scoring_snapshot,
                        algorithm_version = EXCLUDED.algorithm_version,
                        fundamental_snapshot_provenance = EXCLUDED.fundamental_snapshot_provenance,
                        evidence_snapshot_id = EXCLUDED.evidence_snapshot_id,
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
                        run.portfolio_snapshot_history_id,
                        run.weights_version,
                        run.scoring_input_digest,
                        Jsonb(run.scoring_snapshot or {}),
                        run.algorithm_version,
                        Jsonb(run.fundamental_snapshot_provenance or {}),
                        run.evidence_snapshot_id,
                    ),
                ).fetchone()
                run_id = int(row["id"])
                conn.execute("DELETE FROM stock_recommendation_items WHERE run_id = %s", (run_id,))
                for item in items:
                    conn.execute(
                        """
                        INSERT INTO stock_recommendation_items (
                            run_id, symbol, action, rank, score, confidence, sector,
                            reasons, risk_warnings, metrics_snapshot, explanation_json, decision_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            Jsonb(item.get("explanation")) if isinstance(item.get("explanation"), dict) else None,
                            Jsonb(_decision_json(item)),
                        ),
                    )
                conn.commit()
                return self._run_with_items(conn, dict(row)) or _json_ready(dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc


    def _insert_items(self, conn: psycopg.Connection, run_id: int, items: list[dict[str, Any]]) -> None:
        for item in items:
            conn.execute(
                """
                INSERT INTO stock_recommendation_items (
                    run_id, symbol, action, rank, score, confidence, sector,
                    reasons, risk_warnings, metrics_snapshot, explanation_json, decision_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id, item["symbol"], item.get("action", "buy"), item["rank"], item["score"],
                    item["confidence"], item.get("sector"), Jsonb(item.get("reasons") or []),
                    Jsonb(item.get("riskWarnings") or item.get("risk_warnings") or []),
                    Jsonb(item.get("metricsSnapshot") or item.get("metrics_snapshot") or {}),
                    Jsonb(item.get("explanation")) if isinstance(item.get("explanation"), dict) else None,
                    Jsonb(_decision_json(item)),
                ),
            )

    def _run_with_items(self, conn: psycopg.Connection, run: dict[str, Any]) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT * FROM stock_recommendation_items WHERE run_id = %s ORDER BY rank ASC, score DESC",
            (run["id"],),
        ).fetchall()
        payload = _json_ready(run)
        payload["items"] = [_json_ready(dict(row)) for row in rows]
        return payload

    def _evidence_snapshot_with_candidates(
        self, conn: psycopg.Connection, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT * FROM stock_recommendation_evidence_candidates WHERE snapshot_id = %s ORDER BY symbol",
            (snapshot["id"],),
        ).fetchall()
        payload = {
            "id": int(snapshot["id"]),
            "snapshotKey": snapshot["snapshot_key"],
            "slotStart": _json_ready(snapshot["slot_start"]),
            "marketDate": _json_ready(snapshot["market_date"]),
            "sessionMode": snapshot["session_mode"],
            "cutoff": _json_ready(snapshot["cutoff"]),
            "universe": _json_ready(snapshot["universe"]),
            "ruleSetVersion": snapshot["rule_set_version"],
            "sourceDigests": _json_ready(snapshot["source_digests"]),
            "sourceStatus": _json_ready(snapshot["source_status"]),
            "status": snapshot["status"],
            "inputDigest": snapshot["input_digest"],
            "candidates": [],
        }
        for row in rows:
            candidate = dict(row)
            payload["candidates"].append({
                "id": int(candidate["id"]),
                "symbol": candidate["symbol"],
                "sector": candidate["sector"],
                "industry": candidate["industry"],
                "changePercent": _json_ready(candidate["change_percent"]),
                "rawFactors": _json_ready(candidate["raw_factors"]),
                "normalizedFactors": _json_ready(candidate["normalized_factors"]),
                "blockScores": _json_ready(candidate["block_scores"]),
                "baseSetupScore": _json_ready(candidate["base_setup_score"]),
                "evidenceReliability": _json_ready(candidate["evidence_reliability"]),
                "reliabilityComponents": _json_ready(candidate["reliability_components"]),
                "rejectionReasons": _json_ready(candidate["rejection_reasons"]),
                "dailyReturns60": _json_ready(candidate["daily_returns_60"]),
                "marketItem": _json_ready(candidate["market_item"]),
                "narrativeContext": _json_ready(candidate.get("narrative_context") or {}),
                "inputDigest": candidate["input_digest"],
                "evaluatedAt": payload["cutoff"],
            })
        return payload

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)


class InMemoryRecommendationRepository(RecommendationRepository):
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {}
        self.profile_history: list[dict[str, Any]] = []
        self.runs: dict[int, dict[str, Any]] = {}
        self.items: dict[int, list[dict[str, Any]]] = {}
        self._run_id = 0
        self.portfolio_snapshot_history: list[dict[str, Any]] = []
        self.evidence_snapshots: dict[str, dict[str, Any]] = {}
        self._evidence_snapshot_id = 0
        self._evidence_candidate_id = 0
        self.score_profiles: dict[int, dict[str, Any]] = {}
        self._score_profile_id = 0

    def get_profile(self, user_sub: str) -> dict[str, Any] | None:
        row = self.profiles.get(user_sub)
        return _json_ready(deepcopy(row)) if row else None

    def list_profile_user_subs(self) -> list[str]:
        rows = sorted(self.profiles.values(), key=lambda item: (item["updated_at"], item["user_sub"]), reverse=True)
        return [str(row["user_sub"]) for row in rows]

    def get_profile_at(self, user_sub: str, cutoff: datetime) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.profile_history
            if row.get("user_sub") == user_sub
            and (observed := _coerce_datetime(row.get("source_as_of"))) is not None
            and observed <= cutoff
        ]
        rows.sort(
            key=lambda row: _coerce_datetime(row.get("source_as_of"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        if not rows:
            return None
        payload = deepcopy(rows[0]["payload"])
        payload.update(history_id=rows[0]["id"], source_as_of=rows[0]["source_as_of"])
        return _json_ready(payload)

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
        previous_profile = self.profiles.get(profile.user_sub) or {}
        requested = {
            "risk_level": profile.risk_level,
            "recommendation_style": profile.recommendation_style,
            "horizon": profile.horizon,
            "max_drawdown_pct": profile.max_drawdown_pct,
            "preferred_sectors": list(profile.preferred_sectors),
            "excluded_sectors": list(profile.excluded_sectors),
            "excluded_symbols": list(profile.excluded_symbols),
        }
        changed = not previous_profile or any(previous_profile.get(key) != value for key, value in requested.items())
        row = {
            "user_sub": profile.user_sub,
            **requested,
            "active_score_profile_id": previous_profile.get("active_score_profile_id"),
            "profile_revision": int(previous_profile.get("profile_revision") or 0) + (1 if changed else 0),
            "updated_at": datetime.now(timezone.utc) if changed else previous_profile.get("updated_at"),
        }
        self.profiles[profile.user_sub] = deepcopy(row)
        payload = {key: deepcopy(value) for key, value in row.items() if key not in {"user_sub", "updated_at"}}
        previous = next((item for item in reversed(self.profile_history) if item["user_sub"] == profile.user_sub), None)
        if previous is None or previous["payload"] != payload:
            self.profile_history.append({
                "id": len(self.profile_history) + 1,
                "user_sub": profile.user_sub,
                "payload": payload,
                "source_as_of": row["updated_at"],
            })
        return _json_ready(row)

    def list_score_profiles(self, user_sub: str) -> list[dict[str, Any]]:
        rows = [deepcopy(row) for row in self.score_profiles.values() if row["user_sub"] == user_sub]
        rows.sort(key=lambda row: (str(row["name"]).lower(), int(row["id"])))
        return _json_ready(rows)

    def create_score_profile(self, profile: ScoreProfileUpsert, *, max_profiles: int) -> dict[str, Any]:
        owned = [row for row in self.score_profiles.values() if row["user_sub"] == profile.user_sub]
        if len(owned) >= max_profiles:
            raise ValueError("score profile limit reached")
        if any(str(row["name"]).casefold() == profile.name.casefold() for row in owned):
            raise ValueError("score profile name already exists")
        self._score_profile_id += 1
        now = datetime.now(timezone.utc)
        row = {
            "id": self._score_profile_id,
            "user_sub": profile.user_sub,
            "name": profile.name,
            "schema_version": profile.schema_version,
            "block_weights": deepcopy(profile.block_weights),
            "factor_weights": deepcopy(profile.factor_weights),
            "portfolio_weight": profile.portfolio_weight,
            "portfolio_factor_weights": deepcopy(profile.portfolio_factor_weights),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        self.score_profiles[row["id"]] = row
        return _json_ready(deepcopy(row))

    def update_score_profile(self, profile_id: int, profile: ScoreProfileUpsert) -> dict[str, Any] | None:
        row = self.score_profiles.get(profile_id)
        if not row or row["user_sub"] != profile.user_sub:
            return None
        if any(
            candidate["id"] != profile_id
            and candidate["user_sub"] == profile.user_sub
            and str(candidate["name"]).casefold() == profile.name.casefold()
            for candidate in self.score_profiles.values()
        ):
            raise ValueError("score profile name already exists")
        requested = {
            "name": profile.name,
            "schema_version": profile.schema_version,
            "block_weights": profile.block_weights,
            "factor_weights": profile.factor_weights,
            "portfolio_weight": profile.portfolio_weight,
            "portfolio_factor_weights": profile.portfolio_factor_weights,
        }
        if all(row.get(key) == value for key, value in requested.items()):
            return _json_ready(deepcopy(row))
        row.update(
            name=requested["name"],
            schema_version=requested["schema_version"],
            block_weights=deepcopy(requested["block_weights"]),
            factor_weights=deepcopy(requested["factor_weights"]),
            portfolio_weight=requested["portfolio_weight"],
            portfolio_factor_weights=deepcopy(requested["portfolio_factor_weights"]),
            revision=int(row.get("revision") or 1) + 1,
            updated_at=datetime.now(timezone.utc),
        )
        active_profile = self.profiles.get(profile.user_sub)
        if active_profile and active_profile.get("active_score_profile_id") == profile_id:
            active_profile["profile_revision"] = int(active_profile.get("profile_revision") or 1) + 1
            active_profile["updated_at"] = datetime.now(timezone.utc)
        return _json_ready(deepcopy(row))

    def delete_score_profile(self, user_sub: str, profile_id: int) -> bool:
        row = self.score_profiles.get(profile_id)
        if not row or row["user_sub"] != user_sub:
            return False
        profile = self.profiles.get(user_sub)
        if profile and profile.get("active_score_profile_id") == profile_id:
            profile["active_score_profile_id"] = None
            profile["recommendation_style"] = "balanced"
            profile["profile_revision"] = int(profile.get("profile_revision") or 1) + 1
            profile["updated_at"] = datetime.now(timezone.utc)
        del self.score_profiles[profile_id]
        return True

    def activate_score_profile(self, user_sub: str, profile_id: int | None, *, preset_style: str) -> dict[str, Any] | None:
        profile = self.profiles.get(user_sub)
        if not profile:
            return None
        if profile_id is not None:
            row = self.score_profiles.get(profile_id)
            if not row or row["user_sub"] != user_sub:
                return None
        if profile.get("active_score_profile_id") == profile_id and profile.get("recommendation_style") == preset_style:
            return _json_ready(deepcopy(profile))
        profile["active_score_profile_id"] = profile_id
        profile["recommendation_style"] = preset_style
        profile["profile_revision"] = int(profile.get("profile_revision") or 1) + 1
        profile["updated_at"] = datetime.now(timezone.utc)
        return _json_ready(deepcopy(profile))

    def get_portfolio_snapshot(self, user_sub: str) -> dict[str, Any] | None:
        row = getattr(self, "portfolio_snapshots", {}).get(user_sub)
        return _json_ready(deepcopy(row)) if row else None

    def get_portfolio_snapshot_at(self, user_sub: str, cutoff: datetime) -> dict[str, Any] | None:
        rows = []
        for index, row in enumerate(self.portfolio_snapshot_history, start=1):
            if row.get("user_sub") != user_sub:
                continue
            observed = row.get("source_as_of")
            if isinstance(observed, str):
                text = observed[:-1] + "+00:00" if observed.endswith("Z") else observed
                try:
                    observed = datetime.fromisoformat(text)
                except ValueError:
                    continue
            if isinstance(observed, datetime):
                normalized = observed if observed.tzinfo else observed.replace(tzinfo=timezone.utc)
                if normalized <= cutoff:
                    rows.append({**deepcopy(row), "id": index})
        rows.sort(key=lambda item: (str(item.get("source_as_of")), int(item["id"])), reverse=True)
        return _json_ready(rows[0]) if rows else None


    def get_active_weight_set(self) -> dict[str, Any] | None:
        payload = getattr(self, "active_weight_set", None)
        return _json_ready(deepcopy(payload)) if payload else None


    def get_evidence_snapshot(self, snapshot_key: str) -> dict[str, Any] | None:
        row = self.evidence_snapshots.get(snapshot_key)
        return _json_ready(deepcopy(row)) if row else None

    def get_evidence_snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        row = next(
            (item for item in self.evidence_snapshots.values() if int(item.get("id") or 0) == snapshot_id),
            None,
        )
        return _json_ready(deepcopy(row)) if row else None

    def create_evidence_snapshot(
        self, snapshot: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        existing = self.evidence_snapshots.get(snapshot["snapshotKey"])
        if existing:
            return _json_ready(deepcopy(existing))
        self._evidence_snapshot_id += 1
        prepared = []
        for candidate in sorted(candidates, key=lambda row: str(row["symbol"])):
            self._evidence_candidate_id += 1
            prepared.append({
                **deepcopy(candidate),
                "id": self._evidence_candidate_id,
                "evaluatedAt": snapshot["cutoff"],
            })
        row = {
            **deepcopy(snapshot),
            "id": self._evidence_snapshot_id,
            "candidates": prepared,
            "createdAt": datetime.now(timezone.utc),
        }
        self.evidence_snapshots[snapshot["snapshotKey"]] = row
        return _json_ready(deepcopy(row))


    def upsert_portfolio_snapshot(self, user_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self, "portfolio_snapshots"):
            self.portfolio_snapshots: dict[str, dict[str, Any]] = {}
        existing = self.portfolio_snapshots.get(user_sub)
        append_history = (
            existing is None
            or _portfolio_history_payload(existing.get("payload"))
            != _portfolio_history_payload(payload)
        )
        row = {"user_sub": user_sub, "payload": deepcopy(payload), "updated_at": datetime.now(timezone.utc)}
        self.portfolio_snapshots[user_sub] = deepcopy(row)
        if append_history:
            self.portfolio_snapshot_history.append({
                **deepcopy(row),
                "source_as_of": payload.get("asOf") or payload.get("sourceAsOf") or row["updated_at"],
            })
        return _json_ready(row)

    def list_daily_portfolio_snapshots(self, user_sub: str, start_at: str | None = None) -> list[dict[str, Any]]:
        start = _history_datetime(start_at)
        daily: dict[str, tuple[datetime, dict[str, Any]]] = {}
        for row in self.portfolio_snapshot_history:
            if row.get("user_sub") != user_sub:
                continue
            source_as_of = _history_datetime(row.get("source_as_of"))
            if source_as_of is None or (start is not None and source_as_of < start):
                continue
            key = source_as_of.date().isoformat()
            current = daily.get(key)
            if current is None or source_as_of > current[0]:
                daily[key] = (source_as_of, deepcopy(row))
        return [_json_ready(row) for _, row in sorted(daily.values(), key=lambda item: item[0])]

    def latest_run(self, user_sub: str) -> dict[str, Any] | None:
        rows = [row for row in self.runs.values() if row["user_sub"] == user_sub]
        rows.sort(key=lambda item: (item["generated_at"], item["id"]), reverse=True)
        return self._with_items(rows[0]) if rows else None

    def latest_run_for_session(self, user_sub: str, session_mode: str) -> dict[str, Any] | None:
        rows = [
            row for row in self.runs.values()
            if row["user_sub"] == user_sub and (row.get("summary") or {}).get("sessionMode", "regular") == session_mode
        ]
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
            "portfolio_snapshot_history_id": run.portfolio_snapshot_history_id,
            "weights_version": run.weights_version,
            "algorithm_version": run.algorithm_version,
            "fundamental_snapshot_provenance": deepcopy(run.fundamental_snapshot_provenance or {}),
            "evidence_snapshot_id": run.evidence_snapshot_id,
            "scoring_input_digest": run.scoring_input_digest,
            "scoring_snapshot": deepcopy(run.scoring_snapshot or {}),
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
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _portfolio_history_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = deepcopy(value)
    normalized.pop("asOf", None)
    normalized.pop("sourceAsOf", None)
    return normalized




def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decision_json(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": deepcopy(item.get("decision") or {}),
        "sizing": deepcopy(item.get("sizing") or {}),
        "keyEvidence": deepcopy(item.get("keyEvidence") or []),
        "counterEvidence": deepcopy(item.get("counterEvidence")),
        "cautions": deepcopy(item.get("cautions") or []),
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _history_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
