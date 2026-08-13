"""Online backfill and validation for the 0020 ERD expansion migration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg


DEFAULT_BATCH_SIZE = 10_000


class ExpansionValidationError(RuntimeError):
    """Raised when expansion data is not safe to validate or switch."""


@dataclass(frozen=True)
class BackfillSpec:
    name: str
    table: str
    predicate: str
    assignments: str


TYPED_BACKFILLS = (
    BackfillSpec("orders.occurred_at_ts", "orders", "occurred_at_ts IS NULL", "occurred_at_ts = gops_try_timestamptz(target.occurred_at)"),
    BackfillSpec(
        "stock_recommendation_runs.typed_times",
        "stock_recommendation_runs",
        "slot_start_ts IS NULL OR market_date_value IS NULL OR market_snapshot_at IS NULL",
        """slot_start_ts = COALESCE(target.slot_start_ts, gops_try_timestamptz(target.slot_start)),
           market_date_value = COALESCE(target.market_date_value, gops_try_date(target.market_date)),
           market_snapshot_at = COALESCE(target.market_snapshot_at, gops_try_timestamptz(target.market_snapshot_time))""",
    ),
)

USER_BACKFILL_TABLES = (
    ("orders", "user_sub"),
    ("alerts", "user_sub"),
    ("notifications", "user_sub"),
    ("trade_conditions", "user_sub"),
    ("user_notification_preferences", "user_sub"),
    ("user_recommendation_score_profiles", "user_sub"),
    ("user_investment_profiles", "user_sub"),
    ("user_investment_profile_history", "user_sub"),
    ("user_layout_presets", "user_sub"),
    ("user_portfolio_snapshots", "user_sub"),
    ("user_portfolio_snapshot_history", "user_sub"),
    ("trade_decision_check_events", "user_sub"),
    ("order_coach_fill_history", "user_sub"),
    ("stock_recommendation_runs", "user_sub"),
    ("paper_accounts", "user_id"),
    ("paper_account_runs", "user_id"),
    ("paper_positions", "user_id"),
    ("paper_orders", "user_id"),
    ("paper_order_events", "user_id"),
    ("paper_cash_ledger", "user_id"),
)

INSTRUMENT_BACKFILLS = (
    BackfillSpec("orders.instrument_id", "orders", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol, target.market, target.exchange)"),
    BackfillSpec("alerts.instrument_id", "alerts", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol)"),
    BackfillSpec("paper_positions.instrument_id", "paper_positions", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol)"),
    BackfillSpec("paper_orders.instrument_id", "paper_orders", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol, target.market, target.exchange)"),
    BackfillSpec("stock_recommendation_items.instrument_id", "stock_recommendation_items", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol)"),
    BackfillSpec("stock_recommendation_evidence_candidates.instrument_id", "stock_recommendation_evidence_candidates", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol)"),
    BackfillSpec("order_coach_fill_history.instrument_id", "order_coach_fill_history", "instrument_id IS NULL", "instrument_id = gops_ensure_instrument(target.symbol)"),
)


def _preflight_typed_values(conn: psycopg.Connection[Any]) -> dict[str, int]:
    checks = {
        "orders.occurred_at": "SELECT count(*) FROM orders WHERE occurred_at IS NOT NULL AND gops_try_timestamptz(occurred_at) IS NULL",
        "stock_recommendation_runs.slot_start": "SELECT count(*) FROM stock_recommendation_runs WHERE slot_start IS NOT NULL AND gops_try_timestamptz(slot_start) IS NULL",
        "stock_recommendation_runs.market_date": "SELECT count(*) FROM stock_recommendation_runs WHERE market_date IS NOT NULL AND gops_try_date(market_date) IS NULL",
        "stock_recommendation_runs.market_snapshot_time": "SELECT count(*) FROM stock_recommendation_runs WHERE market_snapshot_time IS NOT NULL AND gops_try_timestamptz(market_snapshot_time) IS NULL",
    }
    failures = {name: int(conn.execute(sql).fetchone()[0]) for name, sql in checks.items()}
    invalid = {name: count for name, count in failures.items() if count}
    if invalid:
        raise ExpansionValidationError(f"unparseable legacy date values: {invalid}")
    return failures


def _run_backfill(conn: psycopg.Connection[Any], spec: BackfillSpec, batch_size: int) -> int:
    total = 0
    while True:
        with conn.transaction():
            cursor = conn.execute(
                f"""
                WITH batch AS (
                    SELECT ctid FROM {spec.table}
                    WHERE {spec.predicate}
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE {spec.table} AS target
                SET {spec.assignments}
                FROM batch
                WHERE target.ctid = batch.ctid
                """,
                (batch_size,),
            )
            changed = cursor.rowcount
        total += changed
        if changed < batch_size:
            return total


def _backfill_paper_executions(conn: psycopg.Connection[Any], batch_size: int) -> int:
    total = 0
    while True:
        with conn.transaction():
            cursor = conn.execute(
                """
                WITH batch AS (
                    SELECT order_id
                    FROM paper_orders AS orders
                    WHERE status IN ('filled', 'partially_filled')
                      AND filled_qty > 0
                      AND fill_price > 0
                      AND NOT EXISTS (
                          SELECT 1 FROM paper_executions AS executions
                          WHERE executions.order_id = orders.order_id
                      )
                    ORDER BY order_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                INSERT INTO paper_executions (
                    execution_id, order_id, execution_sequence, quantity, price, fee,
                    quote_event_id, quote_timestamp, executed_at
                )
                SELECT
                    'paper_exec_' || replace(gops_deterministic_uuid('paper-execution', orders.order_id || ':1')::text, '-', ''),
                    orders.order_id, 1, orders.filled_qty, orders.fill_price, 0,
                    orders.quote_event_id, orders.quote_timestamp,
                    COALESCE(orders.filled_at, orders.updated_at)
                FROM paper_orders AS orders
                JOIN batch USING (order_id)
                ON CONFLICT (order_id, execution_sequence) DO NOTHING
                """,
                (batch_size,),
            )
            changed = cursor.rowcount
        total += changed
        if changed < batch_size:
            break

    with conn.transaction():
        conn.execute(
            """
            UPDATE paper_order_events AS event
            SET execution_id = execution.execution_id
            FROM paper_executions AS execution
            WHERE event.execution_id IS NULL
              AND event.order_id = execution.order_id
              AND event.event_type = 'order.filled'
              AND execution.execution_sequence = 1
            """
        )
        conn.execute(
            """
            UPDATE paper_cash_ledger AS ledger
            SET execution_id = execution.execution_id
            FROM paper_executions AS execution
            WHERE ledger.execution_id IS NULL
              AND ledger.order_id = execution.order_id
              AND ledger.event_type = 'order.filled'
              AND execution.execution_sequence = 1
            """
        )
    return total


def run_erd_backfills(conninfo: str, *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    results: dict[str, int] = {}
    with psycopg.connect(conninfo) as conn:
        _preflight_typed_values(conn)
        conn.commit()
        for spec in TYPED_BACKFILLS:
            results[spec.name] = _run_backfill(conn, spec, batch_size)
        for table, legacy_column in USER_BACKFILL_TABLES:
            spec = BackfillSpec(
                f"{table}.app_user_id",
                table,
                f"app_user_id IS NULL AND {legacy_column} IS NOT NULL",
                f"app_user_id = gops_ensure_app_user_identity('legacy_sub', target.{legacy_column})",
            )
            results[spec.name] = _run_backfill(conn, spec, batch_size)
        for spec in INSTRUMENT_BACKFILLS:
            results[spec.name] = _run_backfill(conn, spec, batch_size)

        with conn.transaction():
            trade_conditions = conn.execute(
                """
                UPDATE trade_conditions
                SET paper_order_id = order_id
                WHERE paper_order_id IS NULL AND order_id IS NOT NULL
                """
            ).rowcount
        results["trade_conditions.references"] = trade_conditions
        results["paper_executions"] = _backfill_paper_executions(conn, batch_size)
    return results


def validate_erd_expansion(conninfo: str) -> dict[str, int]:
    """Validate switch criteria that can be checked immediately after backfill."""

    queries = {
        "date_conversion_failures": """
            SELECT
              (SELECT count(*) FROM orders WHERE occurred_at_ts IS NULL) +
              (SELECT count(*) FROM stock_recommendation_runs
               WHERE slot_start_ts IS NULL OR market_date_value IS NULL OR market_snapshot_at IS NULL)
        """,
        "date_mismatches": """
            SELECT
              (SELECT count(*) FROM orders WHERE occurred_at_ts <> gops_try_timestamptz(occurred_at)) +
              (SELECT count(*) FROM stock_recommendation_runs WHERE
                 slot_start_ts <> gops_try_timestamptz(slot_start) OR
                 market_date_value <> gops_try_date(market_date) OR
                 market_snapshot_at <> gops_try_timestamptz(market_snapshot_time))
        """,
        "user_mapping_missing": """
            SELECT
              (SELECT count(*) FROM orders WHERE user_sub IS NOT NULL AND app_user_id IS NULL) +
              (SELECT count(*) FROM alerts WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM notifications WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM trade_conditions WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_notification_preferences WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_recommendation_score_profiles WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_investment_profiles WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_investment_profile_history WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_layout_presets WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_portfolio_snapshots WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM user_portfolio_snapshot_history WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM trade_decision_check_events WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM order_coach_fill_history WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM stock_recommendation_runs WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM paper_accounts WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM paper_account_runs WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM paper_positions WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM paper_orders WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM paper_order_events WHERE app_user_id IS NULL) +
              (SELECT count(*) FROM paper_cash_ledger WHERE app_user_id IS NULL)
        """,
        "user_mapping_mismatch": """
            SELECT
              (SELECT count(*) FROM orders WHERE user_sub IS NOT NULL AND app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM alerts WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM notifications WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM trade_conditions WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_notification_preferences WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_recommendation_score_profiles WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_investment_profiles WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_investment_profile_history WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_layout_presets WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_portfolio_snapshots WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM user_portfolio_snapshot_history WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM trade_decision_check_events WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM order_coach_fill_history WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM stock_recommendation_runs WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_sub)) +
              (SELECT count(*) FROM paper_accounts WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_id)) +
              (SELECT count(*) FROM paper_account_runs WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_id)) +
              (SELECT count(*) FROM paper_positions WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_id)) +
              (SELECT count(*) FROM paper_orders WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_id)) +
              (SELECT count(*) FROM paper_order_events WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_id)) +
              (SELECT count(*) FROM paper_cash_ledger WHERE app_user_id IS DISTINCT FROM gops_deterministic_uuid('gops-app-user', user_id))
        """,
        "instrument_mapping_missing": """
            SELECT
              (SELECT count(*) FROM orders WHERE instrument_id IS NULL) +
              (SELECT count(*) FROM alerts WHERE instrument_id IS NULL) +
              (SELECT count(*) FROM paper_positions WHERE instrument_id IS NULL) +
              (SELECT count(*) FROM paper_orders WHERE instrument_id IS NULL) +
              (SELECT count(*) FROM stock_recommendation_items WHERE instrument_id IS NULL) +
              (SELECT count(*) FROM stock_recommendation_evidence_candidates WHERE instrument_id IS NULL) +
              (SELECT count(*) FROM order_coach_fill_history WHERE instrument_id IS NULL)
        """,
        "instrument_mapping_mismatch": """
            SELECT
              (SELECT count(*) FROM orders WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.')))) +
              (SELECT count(*) FROM alerts WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.')))) +
              (SELECT count(*) FROM paper_positions WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.')))) +
              (SELECT count(*) FROM paper_orders WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.')))) +
              (SELECT count(*) FROM stock_recommendation_items WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.')))) +
              (SELECT count(*) FROM stock_recommendation_evidence_candidates WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.')))) +
              (SELECT count(*) FROM order_coach_fill_history WHERE instrument_id IS DISTINCT FROM gops_deterministic_uuid('gops-instrument', upper(replace(btrim(symbol), '-', '.'))))
        """,
        "paper_execution_mismatch": """
            SELECT count(*) FROM paper_orders AS orders
            WHERE orders.status IN ('filled', 'partially_filled')
              AND orders.filled_qty <> COALESCE((
                  SELECT sum(executions.quantity) FROM paper_executions AS executions
                  WHERE executions.order_id = orders.order_id
              ), 0)
              OR orders.fill_price <> COALESCE((
                  SELECT sum(executions.quantity * executions.price) / NULLIF(sum(executions.quantity), 0)
                  FROM paper_executions AS executions
                  WHERE executions.order_id = orders.order_id
              ), 0)
        """,
    }
    with psycopg.connect(conninfo) as conn:
        results = {name: int(conn.execute(sql).fetchone()[0]) for name, sql in queries.items()}
    failures = {name: count for name, count in results.items() if count}
    if failures:
        raise ExpansionValidationError(f"ERD expansion validation failed: {failures}")
    return results


def validate_erd_constraints(conninfo: str) -> list[str]:
    """Promote NOT VALID constraints after the data checks have reached zero."""

    constraints = (
        ("orders", "orders_occurred_at_ts_required"),
        ("stock_recommendation_runs", "recommendation_runs_slot_start_ts_required"),
        ("stock_recommendation_runs", "recommendation_runs_market_date_value_required"),
        ("stock_recommendation_runs", "recommendation_runs_market_snapshot_at_required"),
        ("trade_conditions", "trade_conditions_paper_order_fk"),
        ("paper_order_events", "paper_order_events_execution_fk"),
        ("paper_cash_ledger", "paper_cash_ledger_execution_fk"),
        ("stock_recommendation_runs", "recommendation_runs_model_version_fk"),
        ("outbox_events", "outbox_events_delivery_status_check"),
        ("outbox_events", "outbox_events_attempt_count_check"),
        ("orders", "orders_side_check"),
        ("orders", "orders_qty_check"),
        ("orders", "orders_price_check"),
        ("orders", "orders_status_check"),
        ("paper_orders", "paper_orders_status_check"),
        ("orders", "orders_app_user_fk"),
        ("alerts", "alerts_app_user_fk"),
        ("notifications", "notifications_app_user_fk"),
        ("trade_conditions", "trade_conditions_app_user_fk"),
        ("user_notification_preferences", "notification_preferences_app_user_fk"),
        ("user_recommendation_score_profiles", "score_profiles_app_user_fk"),
        ("user_investment_profiles", "investment_profiles_app_user_fk"),
        ("user_investment_profile_history", "investment_profile_history_app_user_fk"),
        ("user_layout_presets", "layout_presets_app_user_fk"),
        ("user_portfolio_snapshots", "portfolio_snapshots_app_user_fk"),
        ("user_portfolio_snapshot_history", "portfolio_snapshot_history_app_user_fk"),
        ("trade_decision_check_events", "decision_check_events_app_user_fk"),
        ("order_coach_fill_history", "coach_fill_history_app_user_fk"),
        ("stock_recommendation_runs", "recommendation_runs_app_user_fk"),
        ("paper_accounts", "paper_accounts_app_user_fk"),
        ("paper_account_runs", "paper_account_runs_app_user_fk"),
        ("paper_positions", "paper_positions_app_user_fk"),
        ("paper_orders", "paper_orders_app_user_fk"),
        ("paper_order_events", "paper_order_events_app_user_fk"),
        ("paper_cash_ledger", "paper_cash_ledger_app_user_fk"),
        ("orders", "orders_instrument_fk"),
        ("alerts", "alerts_instrument_fk"),
        ("paper_positions", "paper_positions_instrument_fk"),
        ("paper_orders", "paper_orders_instrument_fk"),
        ("stock_recommendation_items", "recommendation_items_instrument_fk"),
        ("stock_recommendation_evidence_candidates", "evidence_candidates_instrument_fk"),
        ("order_coach_fill_history", "coach_fill_history_instrument_fk"),
    )
    validated: list[str] = []
    with psycopg.connect(conninfo) as conn:
        for table, constraint in constraints:
            with conn.transaction():
                conn.execute(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{constraint}"')
            validated.append(f"{table}.{constraint}")
        with conn.transaction():
            conn.execute("ALTER TABLE orders ALTER COLUMN occurred_at_ts SET NOT NULL")
            conn.execute("ALTER TABLE stock_recommendation_runs ALTER COLUMN slot_start_ts SET NOT NULL")
            conn.execute("ALTER TABLE stock_recommendation_runs ALTER COLUMN market_date_value SET NOT NULL")
            conn.execute("ALTER TABLE stock_recommendation_runs ALTER COLUMN market_snapshot_at SET NOT NULL")
    return validated
