"""PostgreSQL repository for the order reliability workflow."""

from __future__ import annotations

import os
from dataclasses import asdict
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kis_trader.domain.commands import OrderCommand
from kis_trader.domain.envelope import build_order_fill_envelope, build_order_status_envelope
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

from .repository import IdempotencyConflictError, OrderCreationResult, OrderNotFoundError, SubmissionIntent, utc_now_iso


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
                    return OrderCreationResult(False, True, dict(order), response, None)

                conn.execute(
                    """
                    INSERT INTO orders (
                        order_id, request_id, client_order_id, account_alias, market, symbol, side,
                        qty, price, exchange, order_division, status, broker_order_id, reason, occurred_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
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
                    ),
                )
                self._append_order_event(conn, command.order_id, OrderStatus.RECEIVED, None)
                outbox_event_id = self._insert_outbox_event(
                    conn,
                    ORDERS_COMMANDS_TOPIC,
                    command.order_id,
                    OrderStatus.RECEIVED,
                    command.to_envelope(),
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
                return OrderCreationResult(True, False, dict(order), response, outbox_event_id)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,)).fetchone()
            return dict(row) if row else None

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
            SELECT event_id, topic, message_key, message_key AS key, order_id, status, payload, published_at, created_at
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

    def mark_outbox_published(self, event_id: str) -> None:
        with self._connect() as conn:
            with conn.transaction():
                event = conn.execute("SELECT * FROM outbox_events WHERE event_id = %s FOR UPDATE", (event_id,)).fetchone()
                conn.execute("UPDATE outbox_events SET published_at = now() WHERE event_id = %s", (event_id,))
                if event["topic"] == ORDERS_COMMANDS_TOPIC:
                    self._update_order_status(conn, event["order_id"], OrderStatus.PUBLISHED, None)

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
                  (SELECT count(*) FROM audit_logs) AS audit_log_count
                """
            ).fetchone()
            return dict(row)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

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
