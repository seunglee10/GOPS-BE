from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from kis_trader.contracts.order import OrderView, decimal_to_string, now_utc_iso
from kis_trader.contracts.statuses import OrderStatus
from kis_trader.contracts.topics import CanonicalTopics
from kis_trader.orders.storage import (
    IdempotencyMismatchError,
    OrderAcceptanceDraft,
    OrderAcceptanceResult,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE,
    client_order_id UUID NOT NULL UNIQUE,
    account_alias TEXT NOT NULL,
    env TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL UNIQUE,
    request_body_hash TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    exchange TEXT NOT NULL,
    order_division TEXT NOT NULL,
    sell_type TEXT NOT NULL DEFAULT '',
    condition_price TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    broker_order_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_events (
    event_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES orders(order_id),
    request_id UUID NOT NULL,
    client_order_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS broker_submissions (
    submission_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES orders(order_id),
    request_id UUID NOT NULL UNIQUE,
    client_order_id UUID NOT NULL UNIQUE,
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
    order_id UUID REFERENCES orders(order_id),
    topic TEXT NOT NULL,
    message_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS outbox_events_unpublished_idx
    ON outbox_events (created_at)
    WHERE published_at IS NULL;
"""

MIGRATION_SQL = """
ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_order_id UUID;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS env TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS request_body_hash TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sell_type TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS condition_price TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS broker_order_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE order_events ADD COLUMN IF NOT EXISTS client_order_id UUID;

ALTER TABLE broker_submissions ADD COLUMN IF NOT EXISTS client_order_id UUID;
ALTER TABLE broker_submissions ADD COLUMN IF NOT EXISTS redacted_request JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE broker_submissions ADD COLUMN IF NOT EXISTS redacted_response JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE broker_submissions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS order_id UUID;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS orders_client_order_id_uidx
    ON orders (client_order_id)
    WHERE client_order_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS orders_idempotency_key_hash_uidx
    ON orders (idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS broker_submissions_client_order_id_uidx
    ON broker_submissions (client_order_id)
    WHERE client_order_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'orders'
          AND column_name = 'raw_command'
    ) THEN
        ALTER TABLE orders ALTER COLUMN raw_command DROP NOT NULL;
    END IF;
END $$;
"""


@dataclass(frozen=True)
class PostgresOrderRepository:
    database_url: str

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.execute(MIGRATION_SQL)
            conn.commit()

    def truncate_for_tests(self) -> None:
        with self._connect() as conn:
            conn.execute("TRUNCATE outbox_events, broker_submissions, order_events, orders CASCADE")
            conn.commit()

    def accept_order(self, draft: OrderAcceptanceDraft) -> OrderAcceptanceResult:
        with self._connect() as conn:
            with conn.transaction():
                existing = self._find_by_idempotency_hash(conn, draft.idempotency_key_hash, lock=True)
                if existing is not None:
                    if existing["request_body_hash"] != draft.request_body_hash:
                        raise IdempotencyMismatchError("idempotency key body hash mismatch")
                    return OrderAcceptanceResult(order=_row_to_order_view(existing), created=False)

                self._insert_order(conn, draft)
                self._insert_order_event(
                    conn,
                    event_id=str(uuid4()),
                    order_id=draft.order_id,
                    request_id=draft.request_id,
                    client_order_id=draft.client_order_id,
                    event_type="order.received",
                    status=OrderStatus.RECEIVED,
                    account_alias=draft.account_alias,
                    payload={"order_id": draft.order_id, "command": draft.command.to_payload()},
                )
                self._insert_outbox_event(
                    conn,
                    event_id=draft.command_event_id,
                    order_id=draft.order_id,
                    topic=draft.command_topic,
                    message_key=draft.command_message_key,
                    payload=draft.command_payload,
                )
                row = self._find_order_by_id(conn, draft.order_id)
                if row is None:
                    raise RuntimeError("created order could not be read back")
                return OrderAcceptanceResult(order=_row_to_order_view(row), created=True)

    def get_order(self, order_id: str) -> OrderView | None:
        with self._connect() as conn:
            row = self._find_order_by_id(conn, order_id)
        return _row_to_order_view(row) if row is not None else None

    def fetch_pending_outbox(self, *, limit: int) -> list[Any]:
        from kis_trader.outbox.storage import OutboxEvent

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, order_id, topic, message_key, payload
                FROM outbox_events
                WHERE published_at IS NULL
                ORDER BY created_at, event_id
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [
            OutboxEvent(
                event_id=str(row["event_id"]),
                order_id=str(row["order_id"]) if row["order_id"] is not None else None,
                topic=row["topic"],
                message_key=row["message_key"],
                payload=row["payload"],
            )
            for row in rows
        ]

    def mark_outbox_published(self, *, event_id: str, topic: str, order_id: str | None) -> None:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT published_at FROM outbox_events WHERE event_id = %s FOR UPDATE",
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise LookupError(f"outbox event not found: {event_id}")
                if row["published_at"] is not None:
                    return
                conn.execute("UPDATE outbox_events SET published_at = now() WHERE event_id = %s", (event_id,))
                if topic == CanonicalTopics().order_commands and order_id is not None:
                    self._mark_order_published(conn, order_id)

    def record_submission_result(self, record: Any):
        from kis_trader.ledger.storage import SubmissionRecordResult

        with self._connect() as conn:
            with conn.transaction():
                existing = conn.execute(
                    """
                    SELECT submission_id
                    FROM broker_submissions
                    WHERE request_id = %s OR client_order_id = %s
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (record.request_id, record.client_order_id),
                ).fetchone()
                if existing is not None:
                    return SubmissionRecordResult(submission_id=str(existing["submission_id"]), created=False)

                if self._find_order_by_id(conn, record.order_id, lock=True) is None:
                    raise LookupError(f"order not found: {record.order_id}")

                submission_id = str(uuid4())
                conn.execute(
                    """
                    UPDATE orders
                    SET status = %s,
                        broker_order_id = COALESCE(%s, broker_order_id),
                        version = version + 1,
                        updated_at = now()
                    WHERE order_id = %s
                    """,
                    (record.status.value, record.kis_order_id, record.order_id),
                )
                self._insert_order_event(
                    conn,
                    event_id=str(uuid4()),
                    order_id=record.order_id,
                    request_id=record.request_id,
                    client_order_id=record.client_order_id,
                    event_type=record.event_type,
                    status=record.status,
                    account_alias=record.account_alias,
                    payload=record.event_payload,
                )
                conn.execute(
                    """
                    INSERT INTO broker_submissions (
                        submission_id, order_id, request_id, client_order_id, account_alias,
                        status, http_status, kis_msg_cd, kis_order_id, error_type,
                        redacted_request, redacted_response
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        submission_id,
                        record.order_id,
                        record.request_id,
                        record.client_order_id,
                        record.account_alias,
                        record.status.value,
                        record.http_status,
                        record.kis_msg_cd,
                        record.kis_order_id,
                        record.error_type,
                        self._json(record.redacted_request),
                        self._json(record.redacted_response),
                    ),
                )
                self._insert_outbox_event(
                    conn,
                    event_id=record.result_event_id,
                    order_id=record.order_id,
                    topic=record.result_topic,
                    message_key=record.message_key,
                    payload=record.result_payload,
                )
                return SubmissionRecordResult(submission_id=submission_id, created=True)

    def count_rows(self, table_name: str) -> int:
        if table_name not in {"orders", "order_events", "broker_submissions", "outbox_events"}:
            raise ValueError("unsupported table")
        with self._connect() as conn:
            row = conn.execute(f"SELECT count(*) AS count FROM {table_name}").fetchone()
        return int(row["count"])

    def _insert_order(self, conn: Any, draft: OrderAcceptanceDraft) -> None:
        conn.execute(
            """
            INSERT INTO orders (
                order_id, request_id, client_order_id, account_alias, env,
                idempotency_key_hash, request_body_hash,
                market, symbol, side, qty, price, exchange, order_division,
                sell_type, condition_price, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                draft.order_id,
                draft.request_id,
                draft.client_order_id,
                draft.account_alias,
                draft.env,
                draft.idempotency_key_hash,
                draft.request_body_hash,
                draft.command.market,
                draft.command.symbol,
                draft.command.side,
                draft.command.qty,
                draft.command.price,
                draft.command.exchange,
                draft.command.order_division,
                draft.command.sell_type,
                draft.command.condition_price,
                OrderStatus.RECEIVED.value,
            ),
        )

    def _mark_order_published(self, conn: Any, order_id: str) -> None:
        row = self._find_order_by_id(conn, order_id, lock=True)
        if row is None:
            raise LookupError(f"order not found: {order_id}")
        if row["status"] == OrderStatus.PUBLISHED.value:
            return
        if row["status"] != OrderStatus.RECEIVED.value:
            return
        conn.execute(
            """
            UPDATE orders
            SET status = %s,
                version = version + 1,
                updated_at = now()
            WHERE order_id = %s
            """,
            (OrderStatus.PUBLISHED.value, order_id),
        )
        self._insert_order_event(
            conn,
            event_id=str(uuid4()),
            order_id=order_id,
            request_id=str(row["request_id"]),
            client_order_id=str(row["client_order_id"]),
            event_type="order.published",
            status=OrderStatus.PUBLISHED,
            account_alias=row["account_alias"],
            payload={"order_id": order_id, "published_at": now_utc_iso()},
        )

    def _find_by_idempotency_hash(self, conn: Any, idempotency_key_hash: str, *, lock: bool = False) -> Any:
        lock_sql = " FOR UPDATE" if lock else ""
        return conn.execute(
            f"SELECT * FROM orders WHERE idempotency_key_hash = %s{lock_sql}",
            (idempotency_key_hash,),
        ).fetchone()

    def _find_order_by_id(self, conn: Any, order_id: str, *, lock: bool = False) -> Any:
        lock_sql = " FOR UPDATE" if lock else ""
        return conn.execute(
            f"SELECT * FROM orders WHERE order_id = %s{lock_sql}",
            (order_id,),
        ).fetchone()

    def _insert_order_event(
        self,
        conn: Any,
        *,
        event_id: str,
        order_id: str,
        request_id: str,
        client_order_id: str,
        event_type: str,
        status: OrderStatus,
        account_alias: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO order_events (
                event_id, order_id, request_id, client_order_id,
                event_type, status, account_alias, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                order_id,
                request_id,
                client_order_id,
                event_type,
                status.value,
                account_alias,
                self._json(payload),
            ),
        )

    def _insert_outbox_event(
        self,
        conn: Any,
        *,
        event_id: str,
        order_id: str,
        topic: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO outbox_events (event_id, order_id, topic, message_key, payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (event_id, order_id, topic, message_key, self._json(payload)),
        )

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is not installed. Run uv sync first.") from exc
        psycopg_module = cast(Any, psycopg)
        return psycopg_module.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _json(value: dict[str, Any]) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value)


def _row_to_order_view(row: Any) -> OrderView:
    return OrderView(
        order_id=str(row["order_id"]),
        request_id=str(row["request_id"]),
        client_order_id=str(row["client_order_id"]),
        account_alias=row["account_alias"],
        env=row["env"],
        market=row["market"],
        symbol=row["symbol"],
        side=row["side"],
        qty=decimal_to_string(row["qty"]),
        price=decimal_to_string(row["price"]),
        exchange=row["exchange"],
        order_division=row["order_division"],
        status=OrderStatus(row["status"]),
        created_at=_iso_or_none(row.get("created_at")),
        updated_at=_iso_or_none(row.get("updated_at")),
    )


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
