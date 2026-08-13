"""PostgreSQL repository for the order reliability workflow."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kis_trader.domain.commands import OrderCommand
from kis_trader.domain.envelope import build_order_fill_envelope, build_order_status_envelope, enrich_order_envelope_identity
from kis_trader.domain.status import OrderStatus, assert_transition_allowed
from kis_trader.domain.topics import (
    ORDER_EVENTS_TOPIC,
    ORDERS_COMMANDS_TOPIC,
    ORDERS_DLQ_TOPIC,
    ORDERS_FILLS_TOPIC,
    SUBMIT_RESULTS_TOPIC,
    build_order_message_key,
)

FILL_STATUSES = {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
from kis_trader.security.redaction import redact_sensitive

from .fills import canonical_fill_observation
from .repository import IdempotencyConflictError, OrderCreationResult, OrderNotFoundError, SubmissionIntent, utc_now_iso
from .user_context import apply_postgres_user_context


class PostgresOrderRepository:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @classmethod
    def from_env(cls) -> "PostgresOrderRepository":
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

    def create_received_order(
        self,
        *,
        idempotency_key_hash: str,
        body_hash: str,
        command: OrderCommand,
        user_sub: str | None = None,
    ) -> OrderCreationResult:
        with self._connect() as conn:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT * FROM idempotency_requests WHERE key_hash = %s FOR UPDATE",
                    (idempotency_key_hash,),
                ).fetchone()
                if existing is not None:
                    if existing["body_hash"] != body_hash:
                        raise IdempotencyConflictError("idempotency key reused with a different request body")
                    order = conn.execute("SELECT * FROM orders WHERE order_id = %s", (existing["order_id"],)).fetchone()
                    response = dict(existing["response"] or {})
                    response["idempotent_replay"] = True
                    return OrderCreationResult(False, True, _public_order(order), response, None)

                conn.execute(
                    """
                    INSERT INTO orders (
                        order_id, request_id, client_order_id, account_alias, market, symbol, side,
                        qty, price, exchange, order_division, status, broker_order_id, reason, occurred_at,
                        occurred_at_ts, user_sub
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s::timestamptz, %s)
                    """,
                    (
                        command.order_id,
                        command.request_id,
                        command.client_order_id,
                        command.account_alias,
                        command.market,
                        command.symbol,
                        command.side,
                        command.qty,
                        command.price,
                        command.exchange,
                        command.order_division,
                        OrderStatus.RECEIVED.value,
                        command.broker_order_id,
                        command.occurred_at,
                        command.occurred_at,
                        user_sub,
                    ),
                )
                internal_order = conn.execute(
                    "SELECT * FROM orders WHERE order_id = %s",
                    (command.order_id,),
                ).fetchone()
                self._append_order_event(conn, command.order_id, OrderStatus.RECEIVED, None)
                outbox_event_id = self._insert_outbox_event(
                    conn,
                    ORDERS_COMMANDS_TOPIC,
                    command.order_id,
                    OrderStatus.RECEIVED,
                    enrich_order_envelope_identity(command.to_envelope(), dict(internal_order)),
                    build_order_message_key(command.account_alias, command.symbol),
                )
                response = {
                    "order_id": command.order_id,
                    "request_id": command.request_id,
                    "client_order_id": command.client_order_id,
                    "status": OrderStatus.RECEIVED.value,
                    "idempotent_replay": False,
                    "outbox_event_id": outbox_event_id,
                }
                conn.execute(
                    """
                    INSERT INTO idempotency_requests (key_hash, body_hash, order_id, status, response)
                    VALUES (%s, %s, %s, 'COMPLETED', %s)
                    """,
                    (idempotency_key_hash, body_hash, command.order_id, Jsonb(redact_sensitive(response))),
                )
                order = conn.execute("SELECT * FROM orders WHERE order_id = %s", (command.order_id,)).fetchone()
                return OrderCreationResult(True, False, _public_order(order), response, outbox_event_id)

    def find_idempotent_response(self, idempotency_key_hash: str, body_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT body_hash, response FROM idempotency_requests WHERE key_hash = %s",
                (idempotency_key_hash,),
            ).fetchone()
            if row is None or row["body_hash"] != body_hash:
                return None
            return dict(row["response"] or {})

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,)).fetchone()
            return _public_order(row) if row else None

    def list_order_events(self, order_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM order_events WHERE order_id = %s ORDER BY created_at, event_id",
                (order_id,),
            ).fetchall()

    def update_order_status(self, order_id: str, status: OrderStatus, reason: str | None = None) -> str:
        with self._connect() as conn:
            with conn.transaction():
                return self._update_order_status(conn, order_id, status, reason)

    def fetch_pending_outbox(self, limit: int | None = None, topic: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT event_id, topic, message_key, message_key AS key, order_id, status, payload,
                   delivery_status, attempt_count, next_attempt_at, last_error,
                   locked_at, lock_owner, published_at, created_at
            FROM outbox_events
            WHERE published_at IS NULL
        """
        params: list[Any] = []
        if topic is not None:
            query += " AND topic = %s"
            params.append(topic)
        query += " ORDER BY created_at, event_id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def claim_pending_outbox(
        self,
        *,
        worker_id: str,
        limit: int | None = None,
        topic: str | None = None,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        filters = """
            published_at IS NULL
            AND delivery_status IN ('pending', 'retry', 'publishing')
            AND next_attempt_at <= now()
            AND (locked_at IS NULL OR locked_at < now() - (%s * interval '1 second'))
        """
        params: list[Any] = [lease_seconds]
        if topic is not None:
            filters += " AND topic = %s"
            params.append(topic)
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT %s"
            params.append(limit)
        params.append(worker_id)

        query = f"""
            WITH candidates AS (
                SELECT event_id
                FROM outbox_events
                WHERE {filters}
                ORDER BY next_attempt_at, created_at, event_id
                FOR UPDATE SKIP LOCKED
                {limit_sql}
            )
            UPDATE outbox_events AS event
            SET delivery_status = 'publishing',
                attempt_count = event.attempt_count + 1,
                locked_at = now(),
                lock_owner = %s
            FROM candidates
            WHERE event.event_id = candidates.event_id
            RETURNING event.event_id, event.topic, event.message_key,
                      event.message_key AS key, event.order_id, event.status,
                      event.payload, event.delivery_status, event.attempt_count,
                      event.next_attempt_at, event.last_error, event.locked_at,
                      event.lock_owner, event.published_at, event.created_at
        """
        with self._connect() as conn:
            with conn.transaction():
                rows = conn.execute(query, params).fetchall()
        return sorted(rows, key=lambda row: (row["created_at"], row["event_id"]))

    def mark_outbox_published(self, event_id: str, *, worker_id: str | None = None) -> None:
        with self._connect() as conn:
            with conn.transaction():
                event = conn.execute("SELECT * FROM outbox_events WHERE event_id = %s FOR UPDATE", (event_id,)).fetchone()
                if event is None:
                    return
                if worker_id is not None and event["lock_owner"] not in (None, worker_id):
                    return
                conn.execute(
                    """
                    UPDATE outbox_events
                    SET published_at = now(), delivery_status = 'published',
                        locked_at = NULL, lock_owner = NULL, last_error = NULL
                    WHERE event_id = %s
                    """,
                    (event_id,),
                )
                if event["topic"] == ORDERS_COMMANDS_TOPIC:
                    self._update_order_status(conn, event["order_id"], OrderStatus.PUBLISHED, None)

    def mark_outbox_failed(
        self,
        event_id: str,
        error: Exception | str,
        *,
        worker_id: str | None = None,
        retry_delay_seconds: int = 5,
    ) -> None:
        error_text = str(error)[:4000]
        with self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE outbox_events
                    SET delivery_status = 'retry',
                        next_attempt_at = now() + (%s * interval '1 second'),
                        last_error = %s,
                        locked_at = NULL,
                        lock_owner = NULL
                    WHERE event_id = %s
                      AND (%s IS NULL OR lock_owner IS NULL OR lock_owner = %s)
                    """,
                    (retry_delay_seconds, error_text, event_id, worker_id, worker_id),
                )

    def inbox_event_seen(self, consumer_name: str, event_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM inbox_events WHERE consumer_name = %s AND event_id = %s",
                (consumer_name, event_id),
            ).fetchone() is not None

    def record_inbox_event(
        self,
        consumer_name: str,
        event_id: str,
        *,
        payload_digest: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO inbox_events (consumer_name, event_id, payload_digest)
                VALUES (%s, %s, %s)
                ON CONFLICT (consumer_name, event_id) DO NOTHING
                RETURNING event_id
                """,
                (consumer_name, event_id, payload_digest),
            ).fetchone()
            conn.commit()
        return row is not None

    def claim_submission_intent(self, command: OrderCommand) -> SubmissionIntent:
        existing = self.find_submission(command.request_id, command.client_order_id)
        if existing is not None:
            return SubmissionIntent(False, existing)
        submission = {
            "submission_id": f"sub_{uuid4().hex}",
            "request_id": command.request_id,
            "client_order_id": command.client_order_id,
            "order_id": command.order_id,
            "status": "INTENT_RECORDED",
            "redacted_command": redact_sensitive(_json_ready(asdict(command))),
            "redacted_response": None,
            "reason": None,
            "broker_order_id": command.broker_order_id,
        }
        with self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO broker_submissions (
                        submission_id, request_id, client_order_id, order_id, status, redacted_command, broker_order_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        submission["submission_id"],
                        submission["request_id"],
                        submission["client_order_id"],
                        submission["order_id"],
                        submission["status"],
                        Jsonb(submission["redacted_command"]),
                        submission["broker_order_id"],
                    ),
                )
        stored = self.find_submission(command.request_id, command.client_order_id)
        return SubmissionIntent(stored is not None and stored["submission_id"] == submission["submission_id"], stored or submission)

    def find_submission(self, request_id: str, client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM broker_submissions
                WHERE request_id = %s OR client_order_id = %s
                LIMIT 1
                """,
                (request_id, client_order_id),
            ).fetchone()
            return dict(row) if row else None

    def record_submission_result(
        self,
        submission: dict[str, Any],
        status: OrderStatus,
        *,
        response: dict[str, Any] | None = None,
        reason: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        redacted_response = redact_sensitive(response or {})
        with self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE broker_submissions
                    SET status = %s,
                        redacted_response = %s,
                        reason = %s,
                        broker_order_id = COALESCE(%s, broker_order_id),
                        updated_at = now()
                    WHERE submission_id = %s
                    """,
                    (
                        status.value,
                        Jsonb(_json_ready(redacted_response)),
                        reason,
                        broker_order_id,
                        submission["submission_id"],
                    ),
                )
                if broker_order_id:
                    conn.execute("UPDATE orders SET broker_order_id = %s WHERE order_id = %s", (broker_order_id, submission["order_id"]))
                self._update_order_status(conn, submission["order_id"], status, reason)
                order = conn.execute("SELECT * FROM orders WHERE order_id = %s", (submission["order_id"],)).fetchone()
                payload = {
                    "status": status.value,
                    "reason": reason,
                    "broker_order_id": broker_order_id or submission.get("broker_order_id"),
                    "response": redacted_response,
                }
                envelope = build_order_status_envelope(
                    dict(order),
                    event_type="order.submit.resulted",
                    producer="kis-broker-adapter",
                    source=SUBMIT_RESULTS_TOPIC,
                    payload=payload,
                )
                self._insert_outbox_event(
                    conn,
                    SUBMIT_RESULTS_TOPIC,
                    submission["order_id"],
                    status,
                    envelope,
                    build_order_message_key(order["account_alias"], order["symbol"]),
                )

    def record_dlq(self, *, source: str, message: Any, error: Exception) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO dlq_events (source, topic, error_type, error_message, payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (source, ORDERS_DLQ_TOPIC, type(error).__name__, str(error), Jsonb(_json_ready(redact_sensitive(message)))),
            )
            conn.commit()

    def record_audit(self, action: str, order_id: str, reason: str | None = None) -> None:
        with self._connect() as conn:
            order = conn.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,)).fetchone()
            conn.execute(
                """
                INSERT INTO audit_logs (action, order_id, request_id, client_order_id, account_alias, symbol, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    action,
                    order_id,
                    order["request_id"] if order else None,
                    order["client_order_id"] if order else None,
                    order["account_alias"] if order else None,
                    order["symbol"] if order else None,
                    reason,
                ),
            )
            conn.commit()

    def find_orders_by_status(self, statuses: set[OrderStatus]) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM orders WHERE status = ANY(%s)",
                ([status.value for status in statuses],),
            ).fetchall()

    def find_open_orders(self) -> list[dict[str, Any]]:
        terminal = [
            OrderStatus.REJECTED.value,
            OrderStatus.RISK_REJECTED.value,
            OrderStatus.FILLED.value,
            OrderStatus.CANCELED.value,
            OrderStatus.FAILED.value,
        ]
        with self._connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE NOT (status = ANY(%s))", (terminal,)).fetchall()

    def update_reconciled_order(
        self,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
        *,
        execution_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            with conn.transaction():
                if execution_id is not None:
                    conn.execute(
                        """
                        INSERT INTO executions (execution_id, order_id, payload)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (execution_id, order_id, Jsonb(_json_ready(redact_sensitive(payload or {})))),
                    )
                self._update_order_status(conn, order_id, status, reason)
                order = conn.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,)).fetchone()
                observation = canonical_fill_observation(
                    dict(order), payload, execution_id=execution_id
                )
                if observation is not None:
                    self._append_coach_fill(conn, observation)
                envelope = build_order_status_envelope(
                    dict(order),
                    event_type="order.broker.event.reconciled",
                    producer="broker-event-reconciler",
                    source=ORDER_EVENTS_TOPIC,
                    payload=payload or {},
                )
                self._insert_outbox_event(
                    conn,
                    ORDER_EVENTS_TOPIC,
                    order_id,
                    status,
                    envelope,
                    build_order_message_key(order["account_alias"], order["symbol"]),
                )

    def _append_coach_fill(self, conn: psycopg.Connection, observation: dict[str, Any]) -> None:
        latest = conn.execute(
            """
            SELECT observation_version, cumulative_filled_qty
            FROM order_coach_fill_history
            WHERE fill_id = %s
            ORDER BY observation_version DESC
            LIMIT 1
            """,
            (observation["fill_id"],),
        ).fetchone()
        if latest is not None and Decimal(str(latest["cumulative_filled_qty"])) >= observation["cumulative_filled_qty"]:
            return
        version = int(latest["observation_version"]) + 1 if latest else 1
        conn.execute(
            """
            INSERT INTO order_coach_fill_history (
                fill_id, observation_version, user_sub, order_id, source_execution_id,
                symbol, side, cumulative_filled_qty, average_fill_price, status,
                decision_at, filled_at, source_observed_at, source_payload_digest
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                observation["fill_id"],
                version,
                observation["user_sub"],
                observation["order_id"],
                observation["source_execution_id"],
                observation["symbol"],
                observation["side"],
                observation["cumulative_filled_qty"],
                observation["average_fill_price"],
                observation["status"],
                observation["decision_at"],
                observation["filled_at"],
                observation["source_observed_at"],
                observation["source_payload_digest"],
            ),
        )
    def metrics_snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM orders) AS orders_total,
                  (SELECT count(*) FROM outbox_events WHERE published_at IS NULL) AS outbox_unpublished,
                  (SELECT count(*) FROM dlq_events) AS dlq_count,
                  (SELECT count(*) FROM orders WHERE status = 'SUBMIT_FAILED_UNKNOWN') AS submit_failed_unknown_count,
                  (SELECT count(*) FROM orders WHERE status = 'RECONCILIATION_REQUIRED') AS reconciliation_required_count,
                  (SELECT count(*) FROM audit_logs) AS audit_log_count,
                  (SELECT count(*) FROM paper_accounts WHERE seeded_at IS NOT NULL) AS paper_seed_success_count,
                  (SELECT count(*) FROM paper_accounts WHERE seed_suppressed_at IS NOT NULL) AS paper_seed_suppressed_count,
                  (SELECT count(*) FROM paper_accounts
                     WHERE seed_profile IS NULL AND seed_suppressed_at IS NULL) AS paper_seed_unseeded_count,
                  (SELECT count(*) FROM paper_orders
                     WHERE execution_mode = 'simulation' AND status = 'pending') AS simulation_pending_order_count,
                  (SELECT count(*) FROM paper_orders
                     WHERE execution_mode = 'simulation' AND status = 'filled') AS simulation_filled_order_count,
                  (SELECT count(*) FROM paper_orders
                     WHERE execution_mode = 'simulation' AND status = 'cancelled') AS simulation_cancelled_order_count,
                  (SELECT COALESCE(max(sequence), 0) FROM simulation_matcher_checkpoints) AS simulation_matcher_checkpoint,
                  (SELECT EXTRACT(EPOCH FROM (now() - max(updated_at)))
                     FROM simulation_matcher_checkpoints) AS simulation_matcher_checkpoint_age_seconds
                """
            ).fetchone()
            return dict(row)

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.conninfo, row_factory=dict_row)
        apply_postgres_user_context(conn)
        return conn

    def _update_order_status(
        self,
        conn: psycopg.Connection,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
    ) -> str:
        row = conn.execute("SELECT * FROM orders WHERE order_id = %s FOR UPDATE", (order_id,)).fetchone()
        if row is None:
            raise OrderNotFoundError(f"unknown order_id: {order_id}")
        assert_transition_allowed(OrderStatus(row["status"]), status)
        conn.execute(
            "UPDATE orders SET status = %s, reason = %s, updated_at = now() WHERE order_id = %s",
            (status.value, reason, order_id),
        )
        event_id = self._append_order_event(conn, order_id, status, reason)
        if status in FILL_STATUSES:
            order = {**row, "status": status.value}
            self._insert_outbox_event(
                conn,
                ORDERS_FILLS_TOPIC,
                order_id,
                status,
                build_order_fill_envelope(order, reason=reason),
                build_order_message_key(order["account_alias"], order["symbol"]),
            )
        return event_id

    def _append_order_event(
        self,
        conn: psycopg.Connection,
        order_id: str,
        status: OrderStatus,
        reason: str | None,
    ) -> str:
        event_id = f"evt_{uuid4().hex}"
        order = conn.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO order_events (
                event_id, order_id, request_id, client_order_id, account_alias, symbol, status, reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                order_id,
                order["request_id"],
                order["client_order_id"],
                order["account_alias"],
                order["symbol"],
                status.value,
                reason,
            ),
        )
        return event_id

    def _insert_outbox_event(
        self,
        conn: psycopg.Connection,
        topic: str,
        order_id: str,
        status: OrderStatus,
        payload: dict[str, Any],
        message_key: str,
    ) -> str:
        event_id = f"out_{uuid4().hex}"
        conn.execute(
            """
            INSERT INTO outbox_events (event_id, topic, message_key, order_id, status, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (event_id, topic, message_key, order_id, status.value, Jsonb(_json_ready(redact_sensitive(payload)))),
        )
        return event_id


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_ready(child) for child in value]
    return value


def _public_order(row: Any) -> dict[str, Any]:
    public = dict(row)
    occurred_at_ts = public.get("occurred_at_ts")
    if occurred_at_ts is not None:
        public["occurred_at"] = occurred_at_ts.astimezone(timezone.utc).isoformat()
    for internal_column in ("occurred_at_ts", "app_user_id", "instrument_id"):
        public.pop(internal_column, None)
    return public
