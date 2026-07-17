from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


RISK_LEVELS = {"conservative", "balanced", "aggressive"}
RECOMMENDATION_STYLES = {"momentum", "balanced", "stable"}
HORIZONS = {"intraday"}
RUN_TERMINAL_STATUSES = {"completed", "empty", "market_closed", "profile_required", "failed"}
V1_FACTOR_KEYS = {
    "oneDayRelativeStrength",
    "previousSessionStrength",
    "abnormalDollarVolume",
    "closingLocationValue",
    "lastHourRelativeStrength",
    "high52WeekProximity",
    "newsImpact",
    "liquidityQuality",
    "lowVolatilityQuality",
}


class RecommendationSchemaUnavailable(RuntimeError):
    """Raised when recommendation tables have not been migrated yet."""


class RecommendationStateConflict(RuntimeError):
    """Raised when a concurrent V2 refresh advanced the user's state."""


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
    personalization_input_digest: str | None = None
    personalization_snapshot: dict[str, Any] | None = None
    algorithm_version: str = "legacy"
    preference_state_id: int | None = None
    risk_state_id: int | None = None
    fundamental_snapshot_provenance: dict[str, Any] | None = None
    v2_input_digest: str | None = None
    evidence_snapshot_id: int | None = None


class RecommendationRepository:
    def get_profile(self, user_sub: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_profile_user_subs(self) -> list[str]:
        raise NotImplementedError

    def upsert_profile(self, profile: InvestmentProfileUpsert) -> dict[str, Any]:
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

    def get_v2_context(self, user_sub: str, cutoff: datetime) -> dict[str, Any]:
        raise NotImplementedError

    def get_evidence_snapshot(self, snapshot_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def create_evidence_snapshot(
        self, snapshot: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def commit_v2_run(
        self,
        run: RecommendationRunCreate,
        items: list[dict[str, Any]],
        candidate_features: list[dict[str, Any]],
        preference_state: dict[str, Any],
        preference_events: list[dict[str, Any]],
        risk_state: dict[str, Any],
        *,
        expected_preference_state_id: int | None,
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
                        updated_at = now()
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
                conn.commit()
                return _json_ready(dict(row))
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

    def get_v2_context(self, user_sub: str, cutoff: datetime) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                preference = conn.execute(
                    """
                    SELECT * FROM user_recommendation_preference_states
                    WHERE user_sub = %s
                    ORDER BY state_version DESC
                    LIMIT 1
                    """,
                    (user_sub,),
                ).fetchone()
                risk = conn.execute(
                    """
                    SELECT * FROM user_recommendation_risk_states
                    WHERE user_sub = %s
                    ORDER BY state_version DESC
                    LIMIT 1
                    """,
                    (user_sub,),
                ).fetchone()
                snapshots = conn.execute(
                    """
                    SELECT id, user_sub, payload, source_as_of, created_at
                    FROM user_portfolio_snapshot_history
                    WHERE user_sub = %s
                      AND source_as_of <= %s
                      AND source_as_of >= %s - interval '90 days'
                    ORDER BY source_as_of, id
                    """,
                    (user_sub, cutoff, cutoff),
                ).fetchall()
                fills = conn.execute(
                    """
                    WITH fill_deltas AS (
                        SELECT f.*,
                               f.cumulative_filled_qty - COALESCE(
                                   lag(f.cumulative_filled_qty) OVER (
                                       PARTITION BY f.fill_id ORDER BY f.observation_version
                                   ), 0
                               ) AS incremental_filled_qty
                        FROM order_coach_fill_history f
                        WHERE f.user_sub = %s
                          AND f.filled_at <= %s
                          AND f.filled_at >= %s - interval '180 days'
                    )
                    SELECT d.*
                    FROM fill_deltas d
                    LEFT JOIN user_recommendation_preference_events e
                      ON e.fill_history_id = d.id
                    WHERE e.id IS NULL
                    ORDER BY d.decision_at, d.id
                    """,
                    (user_sub, cutoff, cutoff),
                ).fetchall()
                all_fills = conn.execute(
                    """
                    SELECT f.*,
                           f.cumulative_filled_qty - COALESCE(
                               lag(f.cumulative_filled_qty) OVER (
                                   PARTITION BY f.fill_id ORDER BY f.observation_version
                               ), 0
                           ) AS incremental_filled_qty
                    FROM order_coach_fill_history f
                    WHERE f.user_sub = %s
                      AND f.filled_at <= %s
                      AND f.filled_at >= %s - interval '180 days'
                    ORDER BY f.filled_at, f.id
                    """,
                    (user_sub, cutoff, cutoff),
                ).fetchall()
                strengths = conn.execute(
                    """
                    SELECT order_id, max(order_cumulative_strength) AS strength
                    FROM user_recommendation_preference_events
                    WHERE user_sub = %s AND event_status = 'applied'
                    GROUP BY order_id
                    """,
                    (user_sub,),
                ).fetchall()
                prepared = [self._enrich_fill_for_preference(conn, dict(row)) for row in fills]
                return {
                    "preferenceState": _preference_state_payload(dict(preference)) if preference else None,
                    "preferenceStateId": int(preference["id"]) if preference else None,
                    "riskState": _risk_state_payload(dict(risk)) if risk else None,
                    "portfolioSnapshots": [_json_ready(dict(row)) for row in snapshots],
                    "fills": [_json_ready(row) for row in prepared],
                    "allFills": [_json_ready(dict(row)) for row in all_fills],
                    "orderStrengths": {str(row["order_id"]): float(row["strength"]) for row in strengths},
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
                                reliability_components, rejection_reasons, daily_returns_60, market_item, input_digest
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                snapshot_id, candidate["symbol"], candidate["sector"], candidate["industry"],
                                candidate.get("changePercent"), Jsonb(candidate.get("rawFactors") or {}),
                                Jsonb(candidate.get("normalizedFactors") or {}), Jsonb(candidate.get("blockScores") or {}),
                                candidate["baseSetupScore"], candidate["evidenceReliability"],
                                Jsonb(candidate.get("reliabilityComponents") or {}),
                                Jsonb(candidate.get("rejectionReasons") or []), Jsonb(candidate.get("dailyReturns60") or []),
                                Jsonb(candidate.get("marketItem") or {}), candidate["inputDigest"],
                            ),
                        )
                    return self._evidence_snapshot_with_candidates(conn, dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def _enrich_fill_for_preference(self, conn: psycopg.Connection, fill: dict[str, Any]) -> dict[str, Any]:
        feature = conn.execute(
            """
            SELECT f.id AS candidate_feature_id, f.run_id AS candidate_run_id,
                   f.available_factor_scores, f.candidate_mean_scores,
                   ec.id AS evidence_candidate_id
            FROM stock_recommendation_candidate_features f
            JOIN stock_recommendation_runs r ON r.id = f.run_id
            LEFT JOIN stock_recommendation_evidence_candidates ec
              ON ec.snapshot_id = r.evidence_snapshot_id AND ec.symbol = f.symbol
            WHERE f.symbol = %s
              AND r.user_sub = %s
              AND f.evaluated_at <= %s
              AND f.evaluated_at >= %s - interval '24 hours'
            ORDER BY f.evaluated_at DESC, f.id DESC
            LIMIT 1
            """,
            (fill["symbol"], fill["user_sub"], fill["decision_at"], fill["decision_at"]),
        ).fetchone()
        if feature:
            fill.update(
                candidate_feature_id=feature["candidate_feature_id"],
                candidate_run_id=feature["candidate_run_id"],
                evidence_candidate_id=feature["evidence_candidate_id"],
                feature_scores=feature["available_factor_scores"],
                candidate_mean_scores=feature["candidate_mean_scores"],
            )
        else:
            historical = conn.execute(
                """
                SELECT i.run_id, i.metrics_snapshot
                FROM stock_recommendation_items i
                JOIN stock_recommendation_runs r ON r.id = i.run_id
                WHERE i.symbol = %s
                  AND r.user_sub = %s
                  AND r.generated_at <= %s
                  AND r.generated_at >= %s - interval '24 hours'
                  AND i.metrics_snapshot ? 'professionalFactorScores'
                ORDER BY r.generated_at DESC, i.id DESC
                LIMIT 1
                """,
                (fill["symbol"], fill["user_sub"], fill["decision_at"], fill["decision_at"]),
            ).fetchone()
            if historical:
                rows = conn.execute(
                    """
                    SELECT metrics_snapshot->'professionalFactorScores' AS scores
                    FROM stock_recommendation_items
                    WHERE run_id = %s AND metrics_snapshot ? 'professionalFactorScores'
                    """,
                    (historical["run_id"],),
                ).fetchall()
                scores = historical["metrics_snapshot"].get("professionalFactorScores") or {}
                complete_rows = [row["scores"] for row in rows if _complete_v1_scores(row["scores"])]
                if _complete_v1_scores(scores) and complete_rows:
                    fill.update(
                        candidate_run_id=historical["run_id"],
                        feature_scores=scores,
                        candidate_mean_scores=_factor_means(complete_rows),
                        historical_seed=True,
                    )
        equity_row = conn.execute(
            """
            SELECT payload
            FROM user_portfolio_snapshot_history
            WHERE user_sub = %s AND source_as_of <= %s
            ORDER BY source_as_of DESC, id DESC
            LIMIT 1
            """,
            (fill["user_sub"], fill["decision_at"]),
        ).fetchone()
        if equity_row:
            fill["portfolio_equity"] = _portfolio_equity(equity_row["payload"])
        return fill

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
                        personalization_input_digest, personalization_snapshot,
                        algorithm_version, preference_state_id, risk_state_id,
                        fundamental_snapshot_provenance, v2_input_digest, evidence_snapshot_id, generated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (user_sub, run_key) DO UPDATE
                    SET status = EXCLUDED.status,
                        profile_snapshot = EXCLUDED.profile_snapshot,
                        market_snapshot_time = EXCLUDED.market_snapshot_time,
                        summary = EXCLUDED.summary,
                        portfolio_snapshot_history_id = EXCLUDED.portfolio_snapshot_history_id,
                        weights_version = EXCLUDED.weights_version,
                        personalization_input_digest = EXCLUDED.personalization_input_digest,
                        personalization_snapshot = EXCLUDED.personalization_snapshot,
                        algorithm_version = EXCLUDED.algorithm_version,
                        preference_state_id = EXCLUDED.preference_state_id,
                        risk_state_id = EXCLUDED.risk_state_id,
                        fundamental_snapshot_provenance = EXCLUDED.fundamental_snapshot_provenance,
                        v2_input_digest = EXCLUDED.v2_input_digest,
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
                        run.personalization_input_digest,
                        Jsonb(run.personalization_snapshot or {}),
                        run.algorithm_version,
                        run.preference_state_id,
                        run.risk_state_id,
                        Jsonb(run.fundamental_snapshot_provenance or {}),
                        run.v2_input_digest,
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
                            reasons, risk_warnings, metrics_snapshot, explanation_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        ),
                    )
                conn.commit()
                return self._run_with_items(conn, dict(row)) or _json_ready(dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def commit_v2_run(
        self,
        run: RecommendationRunCreate,
        items: list[dict[str, Any]],
        candidate_features: list[dict[str, Any]],
        preference_state: dict[str, Any],
        preference_events: list[dict[str, Any]],
        risk_state: dict[str, Any],
        *,
        expected_preference_state_id: int | None,
    ) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (run.user_sub,))
                    existing = conn.execute(
                        "SELECT * FROM stock_recommendation_runs WHERE user_sub = %s AND run_key = %s",
                        (run.user_sub, run.run_key),
                    ).fetchone()
                    if existing:
                        return self._run_with_items(conn, dict(existing))
                    latest_preference = conn.execute(
                        """
                        SELECT id, state_version FROM user_recommendation_preference_states
                        WHERE user_sub = %s ORDER BY state_version DESC LIMIT 1 FOR UPDATE
                        """,
                        (run.user_sub,),
                    ).fetchone()
                    actual_id = int(latest_preference["id"]) if latest_preference else None
                    if actual_id != expected_preference_state_id:
                        raise RecommendationStateConflict("recommendation preference state advanced concurrently")
                    preference_version = int(latest_preference["state_version"]) + 1 if latest_preference else 1
                    preference_row = conn.execute(
                        """
                        INSERT INTO user_recommendation_preference_states (
                            user_sub, state_version, as_of, event_cutoff, prior_style,
                            prior_weights, long_term_logits, session_logits, effective_weights,
                            long_sample_count, session_sample_count, preference_confidence,
                            preference_model_version, factor_schema_version, input_digest
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            run.user_sub, preference_version, preference_state["asOf"], preference_state["asOf"],
                            preference_state["priorStyle"], Jsonb(preference_state["priorWeights"]),
                            Jsonb(preference_state["longTermLogits"]), Jsonb(preference_state["sessionLogits"]),
                            Jsonb(preference_state["effectiveWeights"]), preference_state["longSampleCount"],
                            preference_state["sessionSampleCount"], preference_state["preferenceConfidence"],
                            preference_state["preferenceModelVersion"], preference_state["factorSchemaVersion"],
                            preference_state["inputDigest"],
                        ),
                    ).fetchone()
                    latest_risk = conn.execute(
                        "SELECT state_version FROM user_recommendation_risk_states WHERE user_sub = %s ORDER BY state_version DESC LIMIT 1",
                        (run.user_sub,),
                    ).fetchone()
                    risk_version = int(latest_risk["state_version"]) + 1 if latest_risk else 1
                    risk_row = conn.execute(
                        """
                        INSERT INTO user_recommendation_risk_states (
                            user_sub, state_version, as_of, preset, observed_risk, effective_budget,
                            evidence_status, data_ranges, risk_policy_version, input_digest
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            run.user_sub, risk_version, risk_state["asOf"], Jsonb(risk_state["preset"]),
                            Jsonb(risk_state["observedRisk"]), Jsonb(risk_state["effectiveBudget"]),
                            Jsonb(risk_state["evidenceStatus"]), Jsonb(risk_state["dataRanges"]),
                            risk_state["riskPolicyVersion"], risk_state["inputDigest"],
                        ),
                    ).fetchone()
                    row = conn.execute(
                        """
                        INSERT INTO stock_recommendation_runs (
                            user_sub, run_key, slot_start, market_date, status, profile_snapshot,
                            market_snapshot_time, summary, portfolio_snapshot_history_id, weights_version,
                            personalization_input_digest, personalization_snapshot, algorithm_version,
                            preference_state_id, risk_state_id, fundamental_snapshot_provenance,
                            v2_input_digest, evidence_snapshot_id, generated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        RETURNING *
                        """,
                        (
                            run.user_sub, run.run_key, run.slot_start, run.market_date, run.status,
                            Jsonb(run.profile_snapshot), run.market_snapshot_time, Jsonb(run.summary),
                            run.portfolio_snapshot_history_id, run.weights_version,
                            run.personalization_input_digest, Jsonb(run.personalization_snapshot or {}),
                            run.algorithm_version, preference_row["id"], risk_row["id"],
                            Jsonb(run.fundamental_snapshot_provenance or {}), run.v2_input_digest,
                            run.evidence_snapshot_id,
                        ),
                    ).fetchone()
                    run_id = int(row["id"])
                    self._insert_items(conn, run_id, items)
                    for feature in candidate_features:
                        conn.execute(
                            """
                            INSERT INTO stock_recommendation_candidate_features (
                                run_id, symbol, evaluated_at, market_factor_scores, fundamental_factor_scores,
                                available_factor_scores, candidate_mean_scores, base_alpha_score, fundamental_score,
                                fundamental_weight, fundamental_status, feature_snapshot_id, feature_schema_version,
                                feature_version, input_digest
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                run_id, feature["symbol"], feature["evaluated_at"],
                                Jsonb(feature["market_factor_scores"]), Jsonb(feature["fundamental_factor_scores"]),
                                Jsonb(feature["available_factor_scores"]), Jsonb(feature["candidate_mean_scores"]),
                                feature["base_alpha_score"], feature.get("fundamental_score"), feature["fundamental_weight"],
                                feature["fundamental_status"], feature.get("feature_snapshot_id"),
                                feature["feature_schema_version"], feature["feature_version"], feature["input_digest"],
                            ),
                        )
                    for event in preference_events:
                        conn.execute(
                            """
                            INSERT INTO user_recommendation_preference_events (
                                fill_history_id, user_sub, order_id, symbol, side, decision_at,
                                candidate_run_id, candidate_feature_id, evidence_candidate_id, event_status, skip_reason,
                                relative_exposure, event_strength, incremental_notional, portfolio_equity,
                                order_cumulative_strength, provenance, event_schema_version, processing_version, input_digest
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (fill_history_id) DO NOTHING
                            """,
                            (
                                event["fill_history_id"], run.user_sub, event["order_id"], event["symbol"], event["side"],
                                event["decision_at"], event.get("candidate_run_id"), event.get("candidate_feature_id"),
                                event.get("evidence_candidate_id"), event["event_status"], event.get("skip_reason"),
                                Jsonb(event.get("relative_exposure") or {}),
                                event.get("event_strength", 0), event.get("incremental_notional"), event.get("portfolio_equity"),
                                event.get("order_cumulative_strength", 0), Jsonb(event.get("provenance") or {}), event["event_schema_version"],
                                event["processing_version"], _digest(event),
                            ),
                        )
                    return self._run_with_items(conn, dict(row))
        except UndefinedTable as exc:
            raise RecommendationSchemaUnavailable("recommendation database migration required") from exc

    def _insert_items(self, conn: psycopg.Connection, run_id: int, items: list[dict[str, Any]]) -> None:
        for item in items:
            conn.execute(
                """
                INSERT INTO stock_recommendation_items (
                    run_id, symbol, action, rank, score, confidence, sector,
                    reasons, risk_warnings, metrics_snapshot, explanation_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id, item["symbol"], item.get("action", "buy"), item["rank"], item["score"],
                    item["confidence"], item.get("sector"), Jsonb(item.get("reasons") or []),
                    Jsonb(item.get("riskWarnings") or item.get("risk_warnings") or []),
                    Jsonb(item.get("metricsSnapshot") or item.get("metrics_snapshot") or {}),
                    Jsonb(item.get("explanation")) if isinstance(item.get("explanation"), dict) else None,
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
                "inputDigest": candidate["input_digest"],
                "evaluatedAt": payload["cutoff"],
            })
        return payload

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)


class InMemoryRecommendationRepository(RecommendationRepository):
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {}
        self.runs: dict[int, dict[str, Any]] = {}
        self.items: dict[int, list[dict[str, Any]]] = {}
        self._run_id = 0
        self.portfolio_snapshot_history: list[dict[str, Any]] = []
        self.v2_fill_history: list[dict[str, Any]] = []
        self.preference_states: list[dict[str, Any]] = []
        self.preference_events: list[dict[str, Any]] = []
        self.risk_states: list[dict[str, Any]] = []
        self.candidate_features: list[dict[str, Any]] = []
        self.evidence_snapshots: dict[str, dict[str, Any]] = {}
        self._evidence_snapshot_id = 0
        self._evidence_candidate_id = 0

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
            "recommendation_style": profile.recommendation_style,
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

    def get_v2_context(self, user_sub: str, cutoff: datetime) -> dict[str, Any]:
        preference_rows = sorted(
            [row for row in self.preference_states if row["user_sub"] == user_sub],
            key=lambda row: row["state_version"],
            reverse=True,
        )
        risk_rows = sorted(
            [row for row in self.risk_states if row["user_sub"] == user_sub],
            key=lambda row: row["state_version"],
            reverse=True,
        )
        processed = {int(row["fill_history_id"]) for row in self.preference_events if row["user_sub"] == user_sub}
        fills: list[dict[str, Any]] = []
        all_fills: list[dict[str, Any]] = []
        previous_qty: dict[str, float] = {}
        for source in sorted(self.v2_fill_history, key=lambda row: (str(row.get("fill_id")), int(row.get("observation_version") or 0))):
            if source.get("user_sub") != user_sub:
                continue
            filled_at = _coerce_datetime(source.get("filled_at"))
            if filled_at is None or not (cutoff - timedelta(days=180) <= filled_at <= cutoff):
                continue
            row = deepcopy(source)
            fill_id = str(row.get("fill_id") or row.get("order_id") or "")
            cumulative = float(row.get("cumulative_filled_qty") or 0)
            row["incremental_filled_qty"] = max(0.0, cumulative - previous_qty.get(fill_id, 0.0))
            previous_qty[fill_id] = max(previous_qty.get(fill_id, 0.0), cumulative)
            all_fills.append(row)
            if int(row.get("id") or 0) not in processed:
                fills.append(self._enrich_memory_fill(row))
        snapshots = []
        for index, row in enumerate(self.portfolio_snapshot_history, start=1):
            observed = _coerce_datetime(row.get("source_as_of"))
            if row.get("user_sub") == user_sub and observed and cutoff - timedelta(days=90) <= observed <= cutoff:
                snapshots.append({**deepcopy(row), "id": row.get("id") or index})
        strengths: dict[str, float] = {}
        for event in self.preference_events:
            if event.get("user_sub") == user_sub and event.get("event_status") == "applied":
                order_id = str(event.get("order_id") or "")
                strengths[order_id] = max(strengths.get(order_id, 0.0), float(event.get("order_cumulative_strength") or 0.0))
        preference = preference_rows[0] if preference_rows else None
        return {
            "preferenceState": deepcopy(preference.get("payload")) if preference else None,
            "preferenceStateId": int(preference["id"]) if preference else None,
            "riskState": deepcopy(risk_rows[0].get("payload")) if risk_rows else None,
            "portfolioSnapshots": _json_ready(snapshots),
            "fills": _json_ready(fills),
            "allFills": _json_ready(all_fills),
            "orderStrengths": strengths,
        }

    def get_evidence_snapshot(self, snapshot_key: str) -> dict[str, Any] | None:
        row = self.evidence_snapshots.get(snapshot_key)
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

    def _enrich_memory_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        decision = _coerce_datetime(fill.get("decision_at"))
        matches = []
        for row in self.candidate_features:
            observed = _coerce_datetime(row.get("evaluated_at"))
            owner = (self.runs.get(int(row.get("run_id") or 0)) or {}).get("user_sub")
            if owner == fill.get("user_sub") and row.get("symbol") == fill.get("symbol") and decision and observed and decision - timedelta(hours=24) <= observed <= decision:
                matches.append(row)
        if matches:
            matches.sort(key=lambda row: _coerce_datetime(row.get("evaluated_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            match = matches[0]
            fill.update(
                candidate_feature_id=match.get("id"),
                candidate_run_id=match.get("run_id"),
                feature_scores=match.get("available_factor_scores"),
                candidate_mean_scores=match.get("candidate_mean_scores"),
            )
            run = self.runs.get(int(match.get("run_id") or 0)) or {}
            evidence_snapshot_id = run.get("evidence_snapshot_id")
            if evidence_snapshot_id is not None:
                evidence = next((
                    candidate
                    for snapshot in self.evidence_snapshots.values()
                    if snapshot.get("id") == evidence_snapshot_id
                    for candidate in snapshot.get("candidates", [])
                    if candidate.get("symbol") == fill.get("symbol")
                ), None)
                if evidence:
                    fill["evidence_candidate_id"] = evidence.get("id")
        else:
            historical_runs = [
                row for row in self.runs.values()
                if row.get("user_sub") == fill.get("user_sub")
                and decision
                and (generated := _coerce_datetime(row.get("generated_at")))
                and decision - timedelta(hours=24) <= generated <= decision
            ]
            historical_runs.sort(
                key=lambda row: _coerce_datetime(row.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            for run in historical_runs:
                run_items = self.items.get(int(run["id"]), [])
                selected = next((item for item in run_items if item.get("symbol") == fill.get("symbol")), None)
                selected_scores = (selected or {}).get("metricsSnapshot", {}).get("professionalFactorScores", {})
                complete_scores = [
                    item.get("metricsSnapshot", {}).get("professionalFactorScores", {})
                    for item in run_items
                    if _complete_v1_scores(item.get("metricsSnapshot", {}).get("professionalFactorScores", {}))
                ]
                if _complete_v1_scores(selected_scores) and complete_scores:
                    fill.update(
                        candidate_run_id=run["id"],
                        feature_scores=selected_scores,
                        candidate_mean_scores=_factor_means(complete_scores),
                        historical_seed=True,
                    )
                    break
        snapshot_matches = []
        for row in self.portfolio_snapshot_history:
            observed = _coerce_datetime(row.get("source_as_of"))
            if row.get("user_sub") == fill.get("user_sub") and decision and observed and observed <= decision:
                snapshot_matches.append(row)
        if snapshot_matches:
            snapshot_matches.sort(key=lambda row: _coerce_datetime(row.get("source_as_of")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            fill["portfolio_equity"] = _portfolio_equity(snapshot_matches[0].get("payload") or {})
        return fill

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
            "personalization_input_digest": run.personalization_input_digest,
            "personalization_snapshot": deepcopy(run.personalization_snapshot or {}),
            "algorithm_version": run.algorithm_version,
            "preference_state_id": run.preference_state_id,
            "risk_state_id": run.risk_state_id,
            "fundamental_snapshot_provenance": deepcopy(run.fundamental_snapshot_provenance or {}),
            "v2_input_digest": run.v2_input_digest,
            "evidence_snapshot_id": run.evidence_snapshot_id,
            "generated_at": datetime.now(timezone.utc),
        }
        self.runs[run_id] = row
        self.items[run_id] = [deepcopy(item) for item in items]
        return self._with_items(row)

    def commit_v2_run(
        self,
        run: RecommendationRunCreate,
        items: list[dict[str, Any]],
        candidate_features: list[dict[str, Any]],
        preference_state: dict[str, Any],
        preference_events: list[dict[str, Any]],
        risk_state: dict[str, Any],
        *,
        expected_preference_state_id: int | None,
    ) -> dict[str, Any]:
        existing = self.get_run_by_key(run.user_sub, run.run_key)
        if existing:
            return existing
        latest = sorted(
            [row for row in self.preference_states if row["user_sub"] == run.user_sub],
            key=lambda row: row["state_version"],
            reverse=True,
        )
        actual_id = int(latest[0]["id"]) if latest else None
        if actual_id != expected_preference_state_id:
            raise RecommendationStateConflict("recommendation preference state advanced concurrently")
        preference_id = len(self.preference_states) + 1
        preference_version = int(latest[0]["state_version"]) + 1 if latest else 1
        self.preference_states.append({
            "id": preference_id,
            "user_sub": run.user_sub,
            "state_version": preference_version,
            "payload": deepcopy(preference_state),
        })
        user_risks = [row for row in self.risk_states if row["user_sub"] == run.user_sub]
        risk_id = len(self.risk_states) + 1
        self.risk_states.append({
            "id": risk_id,
            "user_sub": run.user_sub,
            "state_version": max([int(row["state_version"]) for row in user_risks], default=0) + 1,
            "payload": deepcopy(risk_state),
        })
        run_values = dict(run.__dict__)
        run_values.update(preference_state_id=preference_id, risk_state_id=risk_id)
        stored = self.create_or_replace_run(RecommendationRunCreate(**run_values), items)
        run_id = int(stored["id"])
        for feature in candidate_features:
            self.candidate_features.append({
                **deepcopy(feature),
                "id": len(self.candidate_features) + 1,
                "run_id": run_id,
            })
        for event in preference_events:
            if any(row["fill_history_id"] == event.get("fill_history_id") for row in self.preference_events):
                continue
            self.preference_events.append({
                **deepcopy(event),
                "id": len(self.preference_events) + 1,
                "user_sub": run.user_sub,
            })
        return self._with_items(self.runs[run_id])

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


def _preference_state_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "priorStyle": row.get("prior_style"),
        "priorWeights": row.get("prior_weights") or {},
        "longTermLogits": row.get("long_term_logits") or {},
        "sessionLogits": row.get("session_logits") or {},
        "effectiveWeights": row.get("effective_weights") or {},
        "longSampleCount": row.get("long_sample_count") or 0,
        "sessionSampleCount": row.get("session_sample_count") or 0,
        "preferenceConfidence": row.get("preference_confidence") or 0,
        "preferenceModelVersion": row.get("preference_model_version"),
        "factorSchemaVersion": row.get("factor_schema_version"),
        "inputDigest": row.get("input_digest"),
        "asOf": row.get("as_of"),
    }


def _risk_state_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "preset": row.get("preset") or {},
        "observedRisk": row.get("observed_risk") or {},
        "effectiveBudget": row.get("effective_budget") or {},
        "evidenceStatus": row.get("evidence_status") or {},
        "dataRanges": row.get("data_ranges") or {},
        "riskPolicyVersion": row.get("risk_policy_version"),
        "inputDigest": row.get("input_digest"),
        "asOf": row.get("as_of"),
    }


def _factor_means(rows: list[Any]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            try:
                values.setdefault(str(key), []).append(float(value))
            except (TypeError, ValueError):
                continue
    return {key: statistics.mean(entries) for key, entries in values.items() if entries}


def _complete_v1_scores(value: Any) -> bool:
    if not isinstance(value, dict) or not V1_FACTOR_KEYS.issubset(value):
        return False
    try:
        return all(math.isfinite(float(value[key])) for key in V1_FACTOR_KEYS)
    except (TypeError, ValueError):
        return False


def _portfolio_equity(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    candidates = (
        payload.get("totalValue"),
        payload.get("totalEvaluationAmount"),
        account.get("totalValueForeign"),
        account.get("totalValue"),
    )
    for value in candidates:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    total = 0.0
    for row in positions:
        if not isinstance(row, dict):
            continue
        for key in ("marketValueForeign", "marketValue", "marketValueKrw"):
            try:
                value = float(row.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                total += value
                break
    for value in (payload.get("cash"), payload.get("cashForeign"), account.get("cashForeign"), account.get("cash")):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            total += parsed
            break
    return total or None


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
