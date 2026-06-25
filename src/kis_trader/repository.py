from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .order_contract import OrderCommandEnvelope, OrderStatus
from .redaction import redact_sensitive


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE,
    account_alias TEXT NOT NULL,
    env TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    exchange TEXT NOT NULL,
    order_division TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    raw_command JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_events (
    event_id UUID PRIMARY KEY,
    order_id UUID REFERENCES orders(order_id),
    request_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS broker_submissions (
    submission_id UUID PRIMARY KEY,
    order_id UUID REFERENCES orders(order_id),
    request_id UUID NOT NULL UNIQUE,
    account_alias TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    kis_msg_cd TEXT,
    kis_order_id TEXT,
    error_type TEXT,
    redacted_request JSONB NOT NULL,
    redacted_response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id UUID PRIMARY KEY,
    topic TEXT NOT NULL,
    message_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS outbox_events_unpublished_idx
    ON outbox_events (created_at)
    WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id UUID PRIMARY KEY,
    market TEXT NOT NULL,
    env TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    status TEXT NOT NULL,
    result JSONB NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
"""


@dataclass(frozen=True)
class SubmissionRecord:
    status: OrderStatus
    redacted_request: dict[str, Any]
    redacted_response: dict[str, Any]
    http_status: int | None = None
    kis_msg_cd: str | None = None
    kis_order_id: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    topic: str
    message_key: str
    payload: dict[str, Any]


class PostgresOrderRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def has_submission(self, request_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM broker_submissions WHERE request_id = %s LIMIT 1",
                (request_id,),
            ).fetchone()
        return row is not None

    def begin_submission(self, envelope: OrderCommandEnvelope) -> str:
        from psycopg.types.json import Jsonb

        command = envelope.command
        order_id = str(uuid4())
        now = _now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT order_id FROM orders WHERE request_id = %s",
                (envelope.request_id,),
            ).fetchone()
            if existing is not None:
                existing_order_id = str(existing["order_id"])
                conn.execute(
                    """
                    UPDATE orders
                    SET status = %s,
                        version = version + 1,
                        updated_at = now()
                    WHERE order_id = %s
                    """,
                    (OrderStatus.SUBMITTING.value, existing_order_id),
                )
                self._insert_order_event(
                    conn,
                    order_id=existing_order_id,
                    envelope=envelope,
                    event_type="order.submitting",
                    status=OrderStatus.SUBMITTING,
                    payload={"reason": "resuming_submission"},
                )
                conn.commit()
                return existing_order_id

            conn.execute(
                """
                INSERT INTO orders (
                    order_id, request_id, account_alias, env, market, symbol, side,
                    qty, price, exchange, order_division, status, raw_command,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    envelope.request_id,
                    envelope.account_alias,
                    envelope.env,
                    command.market,
                    command.symbol,
                    command.side,
                    str(command.qty),
                    str(command.price),
                    command.exchange,
                    command.order_division,
                    OrderStatus.SUBMITTING.value,
                    Jsonb(redact_sensitive(_envelope_to_dict(envelope))),
                    now,
                    now,
                ),
            )
            for event_type, status in (
                ("order.received", OrderStatus.RECEIVED),
                ("order.validated", OrderStatus.VALIDATED),
                ("order.submitting", OrderStatus.SUBMITTING),
            ):
                self._insert_order_event(
                    conn,
                    order_id=order_id,
                    envelope=envelope,
                    event_type=event_type,
                    status=status,
                    payload={"command": command.to_payload()},
                )
            conn.commit()
        return order_id

    def record_submission_result(
        self,
        *,
        envelope: OrderCommandEnvelope,
        order_id: str,
        record: SubmissionRecord,
        result_topic: str,
    ) -> None:
        from psycopg.types.json import Jsonb

        event_type = _event_type_for_status(record.status)
        outbox_payload = _submission_outbox_payload(envelope, record)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = %s,
                    broker_order_id = COALESCE(%s, broker_order_id),
                    version = version + 1,
                    updated_at = now()
                WHERE order_id = %s
                """,
                (record.status.value, record.kis_order_id, order_id),
            )
            self._insert_order_event(
                conn,
                order_id=order_id,
                envelope=envelope,
                event_type=event_type,
                status=record.status,
                payload=outbox_payload,
            )
            conn.execute(
                """
                INSERT INTO broker_submissions (
                    submission_id, order_id, request_id, account_alias, status,
                    http_status, kis_msg_cd, kis_order_id, error_type,
                    redacted_request, redacted_response
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    str(uuid4()),
                    order_id,
                    envelope.request_id,
                    envelope.account_alias,
                    record.status.value,
                    record.http_status,
                    record.kis_msg_cd,
                    record.kis_order_id,
                    record.error_type,
                    Jsonb(record.redacted_request),
                    Jsonb(record.redacted_response),
                ),
            )
            self.insert_outbox_event(
                conn,
                topic=result_topic,
                key=envelope.kafka_key,
                payload=outbox_payload,
            )
            conn.commit()

    def record_dlq(
        self,
        *,
        topic: str,
        key: str,
        payload: dict[str, Any],
        original_topic: str,
        partition: int | None,
        offset: int | None,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        with self._connect() as conn:
            self.insert_outbox_event(
                conn,
                topic=topic,
                key=f"{original_topic}:{partition}:{offset}",
                payload={
                    "schema_version": 1,
                    "event_type": "order.dlq.recorded",
                    "event_id": str(uuid4()),
                    "occurred_at": _now_iso(),
                    "original_topic": original_topic,
                    "original_partition": partition,
                    "original_offset": offset,
                    "message_key": key,
                    "error_type": error_type,
                    "error_message": error_message,
                    "retryable": retryable,
                    "payload": redact_sensitive(payload),
                },
            )
            conn.commit()

    def fetch_pending_outbox(self, *, limit: int = 100) -> list[OutboxEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, topic, message_key, payload
                FROM outbox_events
                WHERE published_at IS NULL
                ORDER BY created_at
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [
            OutboxEvent(
                event_id=str(row["event_id"]),
                topic=row["topic"],
                message_key=row["message_key"],
                payload=row["payload"],
            )
            for row in rows
        ]

    def mark_outbox_published(self, event_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE outbox_events SET published_at = now() WHERE event_id = %s",
                (event_id,),
            )
            conn.commit()

    def record_reconciliation_run(
        self,
        *,
        run_id: str,
        market: str,
        env: str,
        account_alias: str,
        status: str,
        result: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reconciliation_runs (
                    run_id, market, env, account_alias, status, result, completed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, now())
                """,
                (run_id, market, env, account_alias, status, Jsonb(redact_sensitive(result))),
            )
            conn.commit()

    def record_broker_order_event(self, *, topic: str, key: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            self.insert_outbox_event(conn, topic=topic, key=key, payload=payload)
            conn.commit()

    def fetch_dlq_event(
        self,
        *,
        topic: str,
        event_id: str | None = None,
        latest: bool = False,
    ) -> dict[str, Any]:
        if not event_id and not latest:
            raise ValueError("event_id or latest=True is required.")
        with self._connect() as conn:
            if event_id:
                row = conn.execute(
                    """
                    SELECT event_id, message_key, payload
                    FROM outbox_events
                    WHERE topic = %s
                      AND event_id = %s
                      AND payload->>'event_type' = 'order.dlq.recorded'
                    """,
                    (topic, event_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT event_id, message_key, payload
                    FROM outbox_events
                    WHERE topic = %s
                      AND payload->>'event_type' = 'order.dlq.recorded'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (topic,),
                ).fetchone()
        if row is None:
            raise LookupError("DLQ event not found.")
        return {
            "event_id": str(row["event_id"]),
            "message_key": row["message_key"],
            "payload": row["payload"],
        }

    def find_unknown_orders(self, *, market: str, env: str, account_alias: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT order_id, request_id, broker_order_id, symbol, qty, price, status
                FROM orders
                WHERE market = %s
                  AND env = %s
                  AND account_alias = %s
                  AND status = %s
                ORDER BY created_at
                """,
                (market, env, account_alias, OrderStatus.SUBMIT_FAILED_UNKNOWN.value),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_reconciled_order(
        self,
        *,
        order_id: str,
        request_id: str,
        account_alias: str,
        status: OrderStatus,
        broker_order_id: str | None,
        payload: dict[str, Any],
        topic: str,
        key: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = %s,
                    broker_order_id = COALESCE(%s, broker_order_id),
                    version = version + 1,
                    updated_at = now()
                WHERE order_id = %s
                """,
                (status.value, broker_order_id, order_id),
            )
            conn.execute(
                """
                INSERT INTO order_events (
                    event_id, order_id, request_id, event_type, status, account_alias, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    str(uuid4()),
                    order_id,
                    request_id,
                    "order.reconciled",
                    status.value,
                    account_alias,
                    self._json(payload),
                ),
            )
            self.insert_outbox_event(conn, topic=topic, key=key, payload=payload)
            conn.commit()

    def insert_outbox_event(self, conn: Any, *, topic: str, key: str, payload: dict[str, Any]) -> str:
        event_id = payload.get("event_id") if isinstance(payload.get("event_id"), str) else str(uuid4())
        conn.execute(
            """
            INSERT INTO outbox_events (event_id, topic, message_key, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (event_id, topic, key, self._json(redact_sensitive(payload))),
        )
        return event_id

    def _insert_order_event(
        self,
        conn: Any,
        *,
        order_id: str,
        envelope: OrderCommandEnvelope,
        event_type: str,
        status: OrderStatus,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO order_events (
                event_id, order_id, request_id, event_type, status, account_alias, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                str(uuid4()),
                order_id,
                envelope.request_id,
                event_type,
                status.value,
                envelope.account_alias,
                self._json(redact_sensitive(payload)),
            ),
        )

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is not installed. Run uv sync first.") from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _json(value: dict[str, Any]) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value)


def _event_type_for_status(status: OrderStatus) -> str:
    return {
        OrderStatus.SUBMITTED: "order.submitted",
        OrderStatus.REJECTED: "order.rejected",
        OrderStatus.SUBMIT_FAILED_UNKNOWN: "order.submit_failed_unknown",
        OrderStatus.PARTIALLY_FILLED: "order.partially_filled",
        OrderStatus.FILLED: "order.filled",
        OrderStatus.CANCELED: "order.canceled",
        OrderStatus.RECONCILIATION_REQUIRED: "order.reconciliation_required",
    }.get(status, "order.updated")


def _submission_outbox_payload(envelope: OrderCommandEnvelope, record: SubmissionRecord) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_type": _event_type_for_status(record.status),
        "event_id": str(uuid4()),
        "request_id": envelope.request_id,
        "occurred_at": _now_iso(),
        "producer": "kis-broker-adapter",
        "env": envelope.env,
        "account_alias": envelope.account_alias,
        "status": record.status.value,
        "payload": {
            "market": envelope.command.market,
            "symbol": envelope.command.symbol,
            "side": envelope.command.side,
            "qty": format(envelope.command.qty, "f"),
            "price": format(envelope.command.price, "f"),
            "exchange": envelope.command.exchange,
            "kis_order_id": record.kis_order_id,
            "kis_msg_cd": record.kis_msg_cd,
            "error_type": record.error_type,
        },
    }


def _envelope_to_dict(envelope: OrderCommandEnvelope) -> dict[str, Any]:
    return {
        "schema_version": envelope.schema_version,
        "event_type": envelope.event_type,
        "event_id": envelope.event_id,
        "request_id": envelope.request_id,
        "occurred_at": envelope.occurred_at,
        "producer": envelope.producer,
        "env": envelope.env,
        "account_alias": envelope.account_alias,
        "payload": envelope.command.to_payload(),
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
