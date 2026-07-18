from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kis_trader.domain.commands import OrderRequest

from .fixture import (
    DEMO_FILLS,
    DEMO_FINAL_CASH,
    DEMO_HOLDINGS,
    HOLDING_BY_SYMBOL,
    SEED_PROFILE,
    configured_seed_profile,
    fallback_price,
    seed_snapshot_history,
)

from .models import (
    CANCELLED_STATUS,
    DEFAULT_STARTING_CASH,
    FILLED_STATUS,
    MAX_ACTIVE_ORDER_SYMBOLS,
    PENDING_STATUS,
    PaperCapacityError,
    PaperIdempotencyConflictError,
    PaperOrderCreationResult,
    PaperOrderError,
    PaperOrderNotFoundError,
    public_order,
)


class PostgresPaperTradingRepository:
    def __init__(
        self,
        conninfo: str,
        *,
        default_starting_cash: Decimal = DEFAULT_STARTING_CASH,
        max_active_order_symbols: int = MAX_ACTIVE_ORDER_SYMBOLS,
        seed_profile: str | None = None,
    ) -> None:
        self.conninfo = conninfo
        self.default_starting_cash = Decimal(default_starting_cash)
        self.max_active_order_symbols = max_active_order_symbols
        self.seed_profile = seed_profile

    @classmethod
    def from_env(cls) -> "PostgresPaperTradingRepository":
        conninfo = os.getenv("DATABASE_URL")
        if not conninfo:
            conninfo = make_conninfo(
                host=os.environ["DATABASE_HOST"],
                port=os.getenv("DATABASE_PORT", "5432"),
                dbname=os.environ["DATABASE_NAME"],
                user=os.environ["DATABASE_USER"],
                password=os.environ["DATABASE_PASSWORD"],
            )
        return cls(
            conninfo,
            default_starting_cash=Decimal(os.getenv("PAPER_STARTING_CASH", str(DEFAULT_STARTING_CASH))),
            max_active_order_symbols=int(os.getenv("PAPER_MAX_ACTIVE_ORDER_SYMBOLS", str(MAX_ACTIVE_ORDER_SYMBOLS))),
            seed_profile=configured_seed_profile(),
        )

    def ensure_account(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.transaction():
                self._ensure_account(conn, user_id)
        return self.account_snapshot(user_id)

    def account_snapshot(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.transaction():
                account, run = self._ensure_account(conn, user_id)
                positions = conn.execute(
                    """
                    SELECT * FROM paper_positions
                    WHERE user_id = %s AND generation = %s AND qty > 0
                    ORDER BY symbol
                    """,
                    (user_id, account["current_generation"]),
                ).fetchall()
                realized_row = conn.execute(
                    """
                    SELECT COALESCE(sum(realized_pnl), 0) AS realized_pnl
                    FROM paper_positions
                    WHERE user_id = %s AND generation = %s
                    """,
                    (user_id, account["current_generation"]),
                ).fetchone()
                open_orders = conn.execute(
                    """
                    SELECT * FROM paper_orders
                    WHERE user_id = %s AND generation = %s AND status = 'pending'
                    ORDER BY created_at DESC, order_id DESC
                    """,
                    (user_id, account["current_generation"]),
                ).fetchall()
                return {
                    "source": "paper",
                    "execution_mode": "paper",
                    "account": {
                        "user_id": user_id,
                        "generation": account["current_generation"],
                        "currency": account["currency"],
                        "starting_cash": run["starting_cash"],
                        "cash_balance": run["cash_balance"],
                        "reserved_cash": run["reserved_cash"],
                        "available_cash": run["cash_balance"] - run["reserved_cash"],
                        "realized_pnl": realized_row["realized_pnl"],
                        "started_at": run["started_at"],
                        "seed_profile": account.get("seed_profile"),
                        "seeded_at": account.get("seeded_at"),
                        "seed_suppressed_at": account.get("seed_suppressed_at"),
                    },
                    "positions": [dict(row) for row in positions],
                    "open_orders": [public_order(dict(row)) for row in open_orders],
                }

    def create_order(
        self,
        *,
        user_id: str,
        idempotency_key_hash: str,
        body_hash: str,
        request: OrderRequest,
        execution_mode: str = "paper",
        simulation_run_id: str | None = None,
        simulation_submitted_sequence: int | None = None,
        virtual_submitted_at: str | None = None,
        order_type: str = "limit",
    ) -> PaperOrderCreationResult:
        with self._connect() as conn:
            with conn.transaction():
                account, run = self._ensure_account(conn, user_id, for_update=True)
                existing = conn.execute(
                    """
                    SELECT * FROM paper_orders
                    WHERE user_id = %s AND idempotency_key_hash = %s
                    FOR UPDATE
                    """,
                    (user_id, idempotency_key_hash),
                ).fetchone()
                if existing is not None:
                    if existing["body_hash"] != body_hash:
                        raise PaperIdempotencyConflictError("idempotency key reused with a different request body")
                    return PaperOrderCreationResult(public_order(dict(existing)), True)

                if execution_mode == "paper":
                    # Admission is global because Alpaca's realtime symbol cap is global.
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext('paper-active-order-symbol-capacity'))")
                    active_row = conn.execute(
                        """
                        SELECT count(DISTINCT symbol) AS symbol_count,
                               bool_or(symbol = %s) AS symbol_exists
                        FROM paper_orders
                        WHERE status = 'pending' AND execution_mode = 'paper'
                        """,
                        (request.symbol,),
                    ).fetchone()
                    if (
                        int(active_row["symbol_count"] or 0) >= self.max_active_order_symbols
                        and not bool(active_row["symbol_exists"])
                    ):
                        raise PaperCapacityError(
                            f"paper order realtime subscription limit reached ({self.max_active_order_symbols} symbols)"
                        )

                position = self._ensure_position(
                    conn,
                    user_id,
                    account["current_generation"],
                    request.symbol,
                    for_update=True,
                )
                if request.side == "buy":
                    required = request.qty * request.price
                    available = run["cash_balance"] - run["reserved_cash"]
                    if required > available:
                        raise PaperOrderError("insufficient paper buying power")
                    conn.execute(
                        """
                        UPDATE paper_account_runs
                        SET reserved_cash = reserved_cash + %s
                        WHERE user_id = %s AND generation = %s
                        """,
                        (required, user_id, account["current_generation"]),
                    )
                    reserved_cash_delta = required
                else:
                    available_qty = position["qty"] - position["reserved_qty"]
                    if request.qty > available_qty:
                        raise PaperOrderError("insufficient paper position quantity")
                    conn.execute(
                        """
                        UPDATE paper_positions
                        SET reserved_qty = reserved_qty + %s, updated_at = now()
                        WHERE user_id = %s AND generation = %s AND symbol = %s
                        """,
                        (request.qty, user_id, account["current_generation"], request.symbol),
                    )
                    reserved_cash_delta = Decimal("0")

                order_id = f"paper_ord_{uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO paper_orders (
                        order_id, user_id, generation, market, symbol, side, qty, limit_price,
                        exchange, order_division, order_type, execution_mode, simulation_run_id,
                        simulation_submitted_sequence, virtual_submitted_at,
                        status, idempotency_key_hash, body_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              'pending', %s, %s)
                    """,
                    (
                        order_id,
                        user_id,
                        account["current_generation"],
                        request.market,
                        request.symbol,
                        request.side,
                        request.qty,
                        request.price,
                        request.exchange,
                        request.order_division,
                        order_type,
                        execution_mode,
                        simulation_run_id,
                        simulation_submitted_sequence,
                        virtual_submitted_at,
                        idempotency_key_hash,
                        body_hash,
                    ),
                )
                row = conn.execute("SELECT * FROM paper_orders WHERE order_id = %s", (order_id,)).fetchone()
                self._append_event(conn, row, "order.created", PENDING_STATUS)
                current_run = self._run(conn, user_id, account["current_generation"])
                self._append_ledger(
                    conn,
                    user_id,
                    account["current_generation"],
                    current_run,
                    "order.reserved",
                    order_id=order_id,
                    reserved_cash_delta=reserved_cash_delta,
                )
                return PaperOrderCreationResult(public_order(dict(row)), False)

    def pretrade(self, user_id: str, request: OrderRequest) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.transaction():
                account, run = self._ensure_account(conn, user_id)
                position = self._ensure_position(
                    conn,
                    user_id,
                    account["current_generation"],
                    request.symbol,
                )
                available_cash = run["cash_balance"] - run["reserved_cash"]
                available_qty = position["qty"] - position["reserved_qty"]
                required = request.qty * request.price
                blocked = required > available_cash if request.side == "buy" else request.qty > available_qty
                rule_id = "paper_buying_power" if request.side == "buy" else "paper_position_quantity"
                explanation = (
                    f"주문 필요 금액 {required} USD가 주문 가능 금액 {available_cash} USD를 초과합니다."
                    if request.side == "buy"
                    else f"매도 수량 {request.qty}주가 주문 가능한 보유수량 {available_qty}주를 초과합니다."
                )
                return {
                    "verdict": "block" if blocked else "allow",
                    "requestedQty": str(request.qty),
                    "adjustedQty": None,
                    "triggeredRules": ([{
                        "ruleId": rule_id,
                        "action": "block",
                        "title": "가상계좌 주문 가능 범위를 초과했습니다",
                        "explanation": explanation,
                        "guidance": "주문 금액 또는 수량을 줄여 주십시오.",
                    }] if blocked else []),
                }

    def get_order(self, user_id: str, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE order_id = %s AND user_id = %s",
                (order_id, user_id),
            ).fetchone()
            return public_order(dict(row)) if row else None

    def list_orders(
        self,
        user_id: str,
        *,
        status: str | None = None,
        include_previous: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        query = """
            SELECT po.* FROM paper_orders po
            JOIN paper_accounts pa ON pa.user_id = po.user_id
            WHERE po.user_id = %s
        """
        params: list[Any] = [user_id]
        if not include_previous:
            query += " AND po.generation = pa.current_generation"
        if status is not None:
            query += " AND po.status = %s"
            params.append(status)
        query += " ORDER BY po.created_at DESC, po.order_id DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            return [public_order(dict(row)) for row in conn.execute(query, params).fetchall()]

    def list_order_events(self, user_id: str, order_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT poe.* FROM paper_order_events poe
                JOIN paper_orders po ON po.order_id = poe.order_id
                WHERE poe.order_id = %s AND po.user_id = %s
                ORDER BY poe.created_at, poe.event_id
                """,
                (order_id, user_id),
            ).fetchall()

    def cancel_order(self, user_id: str, order_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM paper_orders WHERE order_id = %s AND user_id = %s FOR UPDATE",
                    (order_id, user_id),
                ).fetchone()
                if row is None:
                    raise PaperOrderNotFoundError("paper order not found")
                if row["status"] != PENDING_STATUS:
                    return public_order(dict(row))
                run = self._run(conn, user_id, row["generation"], for_update=True)
                reserved_delta = self._release_reservation(conn, row, run)
                conn.execute(
                    """
                    UPDATE paper_orders
                    SET status = 'cancelled', reason = 'cancelled_by_user',
                        cancelled_at = now(), updated_at = now()
                    WHERE order_id = %s
                    """,
                    (order_id,),
                )
                updated = conn.execute("SELECT * FROM paper_orders WHERE order_id = %s", (order_id,)).fetchone()
                self._append_event(conn, updated, "order.cancelled", CANCELLED_STATUS, "cancelled_by_user")
                current_run = self._run(conn, user_id, row["generation"])
                self._append_ledger(
                    conn,
                    user_id,
                    row["generation"],
                    current_run,
                    "order.reservation_released",
                    order_id=order_id,
                    reserved_cash_delta=reserved_delta,
                )
                return public_order(dict(updated))

    def reset_account(self, user_id: str, starting_cash: Decimal) -> dict[str, Any]:
        starting_cash = Decimal(starting_cash)
        if starting_cash <= 0:
            raise PaperOrderError("starting_cash must be positive")
        with self._connect() as conn:
            with conn.transaction():
                account, run = self._ensure_account(
                    conn,
                    user_id,
                    for_update=True,
                    apply_seed=False,
                )
                self._rotate_account(
                    conn,
                    user_id,
                    starting_cash,
                    suppress_seed=True,
                    current=(account, run),
                )
        return self.account_snapshot(user_id)

    def match_quote(
        self,
        *,
        symbol: str,
        bid_price: Decimal | None,
        ask_price: Decimal | None,
        quote_timestamp: str | None,
        quote_event_id: str | None,
        execution_mode: str = "paper",
        simulation_run_id: str | None = None,
        quote_sequence: int | None = None,
        virtual_timestamp: str | None = None,
    ) -> list[dict[str, Any]]:
        symbol = symbol.upper()
        bid = Decimal(bid_price) if bid_price is not None else None
        ask = Decimal(ask_price) if ask_price is not None else None
        matched: list[dict[str, Any]] = []
        with self._connect() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT * FROM paper_orders
                    WHERE symbol = %s AND status = 'pending' AND execution_mode = %s
                      AND (%s <> 'simulation' OR simulation_run_id = %s)
                      AND (%s::bigint IS NULL OR simulation_submitted_sequence IS NULL
                           OR (order_type = 'market' AND simulation_submitted_sequence <= %s::bigint)
                           OR (order_type = 'limit' AND simulation_submitted_sequence < %s::bigint))
                    ORDER BY created_at, order_id
                    FOR UPDATE SKIP LOCKED
                    """,
                    (symbol, execution_mode, execution_mode, simulation_run_id, quote_sequence, quote_sequence, quote_sequence),
                ).fetchall()
                for row in rows:
                    fill_price = ask if row["side"] == "buy" and ask is not None and ask <= row["limit_price"] else None
                    if row["side"] == "sell" and bid is not None and bid >= row["limit_price"]:
                        fill_price = bid
                    if fill_price is None or fill_price <= 0:
                        continue

                    run = self._run(conn, row["user_id"], row["generation"], for_update=True)
                    position = self._ensure_position(
                        conn,
                        row["user_id"],
                        row["generation"],
                        row["symbol"],
                        for_update=True,
                    )
                    qty = row["qty"]
                    if row["side"] == "buy":
                        reserved = qty * row["limit_price"]
                        cost = qty * fill_price
                        next_qty = position["qty"] + qty
                        next_average = ((position["qty"] * position["average_price"]) + cost) / next_qty
                        conn.execute(
                            """
                            UPDATE paper_positions
                            SET qty = %s, average_price = %s, updated_at = now()
                            WHERE user_id = %s AND generation = %s AND symbol = %s
                            """,
                            (next_qty, next_average, row["user_id"], row["generation"], row["symbol"]),
                        )
                        conn.execute(
                            """
                            UPDATE paper_account_runs
                            SET cash_balance = cash_balance - %s,
                                reserved_cash = reserved_cash - %s
                            WHERE user_id = %s AND generation = %s
                            """,
                            (cost, reserved, row["user_id"], row["generation"]),
                        )
                        cash_delta = -cost
                        reserved_delta = -reserved
                    else:
                        proceeds = qty * fill_price
                        realized = (fill_price - position["average_price"]) * qty
                        conn.execute(
                            """
                            UPDATE paper_positions
                            SET qty = qty - %s, reserved_qty = reserved_qty - %s,
                                realized_pnl = realized_pnl + %s, updated_at = now()
                            WHERE user_id = %s AND generation = %s AND symbol = %s
                            """,
                            (qty, qty, realized, row["user_id"], row["generation"], row["symbol"]),
                        )
                        conn.execute(
                            """
                            UPDATE paper_account_runs SET cash_balance = cash_balance + %s
                            WHERE user_id = %s AND generation = %s
                            """,
                            (proceeds, row["user_id"], row["generation"]),
                        )
                        cash_delta = proceeds
                        reserved_delta = Decimal("0")

                    conn.execute(
                        """
                        UPDATE paper_orders
                        SET status = 'filled', filled_qty = qty, fill_price = %s,
                            quote_event_id = %s, quote_timestamp = %s,
                            filled_at = now(), virtual_filled_at = %s,
                            updated_at = now(), reason = NULL
                        WHERE order_id = %s
                        """,
                        (fill_price, quote_event_id, quote_timestamp, virtual_timestamp, row["order_id"]),
                    )
                    updated = conn.execute("SELECT * FROM paper_orders WHERE order_id = %s", (row["order_id"],)).fetchone()
                    self._append_event(
                        conn,
                        updated,
                        "order.filled",
                        FILLED_STATUS,
                        payload={
                            "fill_price": str(fill_price),
                            "quote_event_id": quote_event_id,
                            "quote_timestamp": quote_timestamp,
                        },
                    )
                    current_run = self._run(conn, row["user_id"], row["generation"])
                    self._append_ledger(
                        conn,
                        row["user_id"],
                        row["generation"],
                        current_run,
                        "order.filled",
                        order_id=row["order_id"],
                        cash_delta=cash_delta,
                        reserved_cash_delta=reserved_delta,
                    )
                    self._append_portfolio_snapshot(
                        conn,
                        row["user_id"],
                        row["generation"],
                        virtual_timestamp or quote_timestamp,
                        {symbol: fill_price},
                    )
                    matched.append(public_order(dict(updated)))
        return matched

    def cancel_simulation_run(self, run_id: str, *, reason: str = "simulation_run_ended") -> list[dict[str, Any]]:
        cancelled: list[dict[str, Any]] = []
        with self._connect() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT * FROM paper_orders
                    WHERE execution_mode = 'simulation' AND simulation_run_id = %s AND status = 'pending'
                    ORDER BY created_at, order_id
                    FOR UPDATE
                    """,
                    (run_id,),
                ).fetchall()
                for row in rows:
                    run = self._run(conn, row["user_id"], row["generation"], for_update=True)
                    reserved_delta = self._release_reservation(conn, row, run)
                    conn.execute(
                        """
                        UPDATE paper_orders
                        SET status = 'cancelled', reason = %s, cancelled_at = now(), updated_at = now()
                        WHERE order_id = %s
                        """,
                        (reason, row["order_id"]),
                    )
                    updated = conn.execute(
                        "SELECT * FROM paper_orders WHERE order_id = %s", (row["order_id"],)
                    ).fetchone()
                    self._append_event(conn, updated, "order.cancelled", CANCELLED_STATUS, reason)
                    current_run = self._run(conn, row["user_id"], row["generation"])
                    self._append_ledger(
                        conn, row["user_id"], row["generation"], current_run,
                        "order.reservation_released", order_id=row["order_id"],
                        reserved_cash_delta=reserved_delta,
                    )
                    cancelled.append(public_order(dict(updated)))
                conn.execute(
                    """
                    WITH canceled AS (
                        UPDATE trade_conditions
                        SET status = 'canceled', updated_at = now(), version = version + 1
                        WHERE execution_mode = 'simulation' AND simulation_run_id = %s
                          AND status IN ('watching', 'paused', 'triggered', 'executing')
                        RETURNING alert_id
                    )
                    UPDATE alerts SET status = 'disabled'
                    WHERE id IN (SELECT alert_id FROM canceled)
                    """,
                    (run_id,),
                )
        return cancelled

    def active_order_symbols(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM paper_orders WHERE status = 'pending' AND execution_mode = 'paper' ORDER BY symbol"
            ).fetchall()
            return [str(row["symbol"]) for row in rows]

    def active_position_symbols(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT pp.symbol
                FROM paper_positions pp
                JOIN paper_accounts pa
                  ON pa.user_id = pp.user_id AND pa.current_generation = pp.generation
                WHERE pp.qty > 0
                ORDER BY pp.symbol
                """
            ).fetchall()
            return [str(row["symbol"]) for row in rows]

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def _ensure_account(
        self,
        conn: psycopg.Connection,
        user_id: str,
        *,
        for_update: bool = False,
        apply_seed: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        created = conn.execute(
            """
            INSERT INTO paper_accounts (user_id, current_generation, currency)
            VALUES (%s, 1, 'USD') ON CONFLICT DO NOTHING
            RETURNING user_id
            """,
            (user_id,),
        ).fetchone()
        account = conn.execute(
            f"SELECT * FROM paper_accounts WHERE user_id = %s{' FOR UPDATE' if for_update else ''}",
            (user_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO paper_account_runs (
                user_id, generation, starting_cash, cash_balance, reserved_cash, status
            ) VALUES (%s, %s, %s, %s, 0, 'active')
            ON CONFLICT DO NOTHING
            """,
            (user_id, account["current_generation"], self.default_starting_cash, self.default_starting_cash),
        )
        run = self._run(conn, user_id, account["current_generation"], for_update=for_update)
        if created is not None:
            self._append_ledger(
                conn,
                user_id,
                account["current_generation"],
                run,
                "account.opened",
                cash_delta=self.default_starting_cash,
            )
        if apply_seed:
            self._maybe_seed_account(conn, dict(account), dict(run))
        account = conn.execute(
            f"SELECT * FROM paper_accounts WHERE user_id = %s{' FOR UPDATE' if for_update else ''}",
            (user_id,),
        ).fetchone()
        run = self._run(conn, user_id, account["current_generation"], for_update=for_update)
        return dict(account), dict(run)

    def _maybe_seed_account(
        self,
        conn: psycopg.Connection,
        account: dict[str, Any],
        run: dict[str, Any],
    ) -> None:
        if self.seed_profile != SEED_PROFILE:
            return
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1818))", (account["user_id"],))
        account = dict(conn.execute(
            "SELECT * FROM paper_accounts WHERE user_id = %s FOR UPDATE", (account["user_id"],)
        ).fetchone())
        if account.get("seed_suppressed_at") or account.get("seed_profile") == SEED_PROFILE:
            return
        user_id = account["user_id"]
        generation = account["current_generation"]
        state = conn.execute(
            """
            SELECT
              EXISTS(
                SELECT 1 FROM paper_orders WHERE user_id = %s AND generation = %s
              ) AS has_orders,
              EXISTS(
                SELECT 1 FROM paper_positions WHERE user_id = %s AND generation = %s AND qty > 0
              ) AS has_positions
            """,
            (user_id, generation, user_id, generation),
        ).fetchone()
        current_run = self._run(conn, user_id, generation, for_update=True)
        if (
            state["has_orders"] or state["has_positions"]
            or current_run["cash_balance"] != self.default_starting_cash
            or current_run["reserved_cash"] != 0
        ):
            account, current_run = self._rotate_account(
                conn,
                user_id,
                self.default_starting_cash,
                suppress_seed=False,
                current=(account, current_run),
            )
            generation = account["current_generation"]
        conn.execute(
            """
            UPDATE paper_account_runs SET cash_balance = %s, reserved_cash = 0
            WHERE user_id = %s AND generation = %s
            """,
            (DEMO_FINAL_CASH, user_id, generation),
        )
        realized_by_symbol = {
            "GOOGL": Decimal("211.20"), "XOM": Decimal("127.50"),
            "HD": Decimal("-84.00"), "AMZN": Decimal("50.00"),
        }
        for holding in DEMO_HOLDINGS:
            conn.execute(
                """
                INSERT INTO paper_positions (
                    user_id, generation, symbol, qty, reserved_qty, average_price, realized_pnl, updated_at
                ) VALUES (%s, %s, %s, %s, 0, %s, %s, now())
                ON CONFLICT (user_id, generation, symbol) DO UPDATE
                SET qty = EXCLUDED.qty, reserved_qty = 0, average_price = EXCLUDED.average_price,
                    realized_pnl = EXCLUDED.realized_pnl, updated_at = now()
                """,
                (
                    user_id, generation, holding.symbol, holding.quantity, holding.average_price,
                    realized_by_symbol.get(holding.symbol, Decimal("0")),
                ),
            )
        seed_cash = self.default_starting_cash
        for index, fill in enumerate(DEMO_FILLS, start=1):
            seed_key = f"{SEED_PROFILE}:{user_id}:{generation}:{index}"
            order_id = f"paper_seed_{uuid5(NAMESPACE_URL, seed_key).hex}"
            conn.execute(
                """
                INSERT INTO paper_orders (
                    order_id, user_id, generation, market, symbol, side, qty, limit_price,
                    exchange, order_division, order_type, execution_mode, seed_profile,
                    status, filled_qty, fill_price, quote_event_id, quote_timestamp,
                    idempotency_key_hash, body_hash, created_at, updated_at, filled_at
                ) VALUES (
                    %s, %s, %s, 'overseas', %s, %s, %s, %s,
                    %s, '00', 'limit', 'paper', %s,
                    'filled', %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) ON CONFLICT (order_id) DO NOTHING
                """,
                (
                    order_id, user_id, generation, fill.symbol, fill.side, fill.quantity, fill.price,
                    HOLDING_BY_SYMBOL[fill.symbol].exchange, SEED_PROFILE,
                    fill.quantity, fill.price, f"seed:{SEED_PROFILE}:{generation}:{index}", fill.filled_at,
                    f"seed:{SEED_PROFILE}:{generation}:{index}", SEED_PROFILE,
                    fill.filled_at, fill.filled_at, fill.filled_at,
                ),
            )
            order = conn.execute("SELECT * FROM paper_orders WHERE order_id = %s", (order_id,)).fetchone()
            self._append_event(
                conn, order, "order.filled", FILLED_STATUS,
                payload={"seed_profile": SEED_PROFILE, "fill_price": str(fill.price)},
            )
            cash_delta = fill.quantity * fill.price * (Decimal("-1") if fill.side == "buy" else Decimal("1"))
            seed_cash += cash_delta
            conn.execute(
                """
                INSERT INTO paper_cash_ledger (
                    entry_id, user_id, generation, order_id, event_type,
                    cash_delta, reserved_cash_delta, cash_balance_after,
                    reserved_cash_after, payload, created_at
                ) VALUES (%s, %s, %s, %s, 'order.filled', %s, 0, %s, 0, %s, %s)
                ON CONFLICT (entry_id) DO NOTHING
                """,
                (
                    f"paper_seed_led_{uuid5(NAMESPACE_URL, f'{seed_key}:ledger').hex}",
                    user_id, generation, order_id, cash_delta, seed_cash,
                    Jsonb({"seed_profile": SEED_PROFILE}), fill.filled_at,
                ),
            )
        for source_as_of, snapshot in seed_snapshot_history():
            conn.execute(
                """
                INSERT INTO user_portfolio_snapshot_history (user_sub, payload, source_as_of)
                VALUES (%s, %s, %s)
                """,
                (user_id, Jsonb(json.loads(json.dumps(snapshot, default=str))), source_as_of),
            )
        conn.execute(
            """
            UPDATE paper_accounts
            SET seed_profile = %s, seeded_at = now(), seed_suppressed_at = NULL, updated_at = now()
            WHERE user_id = %s
            """,
            (SEED_PROFILE, user_id),
        )
        seeded_run = self._run(conn, user_id, generation)
        self._append_ledger(
            conn, user_id, generation, seeded_run, "account.seeded",
            cash_delta=Decimal("0"),
        )
        if seed_cash != DEMO_FINAL_CASH:
            raise AssertionError("demo fixture cash invariant failed")

    def _rotate_account(
        self,
        conn: psycopg.Connection,
        user_id: str,
        starting_cash: Decimal,
        *,
        suppress_seed: bool,
        current: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        account, _run = current or self._ensure_account(conn, user_id, for_update=True)
        generation = account["current_generation"]
        reason = "account_reset" if suppress_seed else "account_demo_profile"
        pending = conn.execute(
            """
            SELECT * FROM paper_orders
            WHERE user_id = %s AND generation = %s AND status = 'pending'
            FOR UPDATE
            """,
            (user_id, generation),
        ).fetchall()
        for row in pending:
            conn.execute(
                """
                UPDATE paper_orders
                SET status = 'cancelled', reason = %s,
                    cancelled_at = now(), updated_at = now()
                WHERE order_id = %s
                """,
                (reason, row["order_id"]),
            )
            cancelled = {**dict(row), "status": CANCELLED_STATUS, "reason": reason}
            self._append_event(conn, cancelled, "order.cancelled", CANCELLED_STATUS, reason)
        conn.execute(
            """
            UPDATE paper_positions SET reserved_qty = 0, updated_at = now()
            WHERE user_id = %s AND generation = %s
            """,
            (user_id, generation),
        )
        conn.execute(
            """
            UPDATE paper_account_runs
            SET reserved_cash = 0, status = 'archived', ended_at = now()
            WHERE user_id = %s AND generation = %s
            """,
            (user_id, generation),
        )
        next_generation = generation + 1
        conn.execute(
            """
            INSERT INTO paper_account_runs (
                user_id, generation, starting_cash, cash_balance, reserved_cash, status
            ) VALUES (%s, %s, %s, %s, 0, 'active')
            """,
            (user_id, next_generation, starting_cash, starting_cash),
        )
        conn.execute(
            """
            UPDATE paper_accounts
            SET current_generation = %s,
                seed_profile = NULL,
                seeded_at = NULL,
                seed_suppressed_at = CASE WHEN %s THEN now() ELSE NULL END,
                updated_at = now()
            WHERE user_id = %s
            """,
            (next_generation, suppress_seed, user_id),
        )
        new_run = self._run(conn, user_id, next_generation)
        self._append_ledger(
            conn,
            user_id,
            next_generation,
            new_run,
            "account.reset" if suppress_seed else "account.seed_profile_upgraded",
            cash_delta=starting_cash,
        )
        updated_account = conn.execute(
            "SELECT * FROM paper_accounts WHERE user_id = %s FOR UPDATE",
            (user_id,),
        ).fetchone()
        return dict(updated_account), dict(new_run)

    def _run(
        self,
        conn: psycopg.Connection,
        user_id: str,
        generation: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = conn.execute(
            f"""
            SELECT * FROM paper_account_runs
            WHERE user_id = %s AND generation = %s{' FOR UPDATE' if for_update else ''}
            """,
            (user_id, generation),
        ).fetchone()
        if row is None:
            raise PaperOrderError("paper account run not found")
        return dict(row)

    def _append_portfolio_snapshot(
        self,
        conn: psycopg.Connection,
        user_id: str,
        generation: int,
        as_of: str | None,
        price_overrides: dict[str, Decimal],
    ) -> None:
        run = self._run(conn, user_id, generation)
        rows = conn.execute(
            """
            SELECT symbol, qty, average_price
            FROM paper_positions
            WHERE user_id = %s AND generation = %s AND qty > 0
            ORDER BY symbol
            """,
            (user_id, generation),
        ).fetchall()
        positions: list[dict[str, Any]] = []
        market_value = Decimal("0")
        holdings_cost = Decimal("0")
        for row in rows:
            price = price_overrides.get(row["symbol"]) or fallback_price(row["symbol"]) or row["average_price"]
            value = row["qty"] * price
            cost = row["qty"] * row["average_price"]
            market_value += value
            holdings_cost += cost
            positions.append({
                "symbol": row["symbol"],
                "quantity": row["qty"],
                "averagePrice": row["average_price"],
                "currentPrice": price,
                "marketValueForeign": value,
                "purchaseAmountForeign": cost,
            })
        equity = run["cash_balance"] + market_value
        payload = {
            "asOf": as_of,
            "source": "account-history",
            "account": {
                "cashForeign": run["cash_balance"],
                "stockValueForeign": market_value,
                "totalValueForeign": equity,
                "unrealizedPnlForeign": market_value - holdings_cost,
            },
            "positions": positions,
        }
        conn.execute(
            """
            INSERT INTO user_portfolio_snapshot_history (user_sub, payload, source_as_of)
            VALUES (%s, %s, COALESCE(%s::timestamptz, now()))
            """,
            (user_id, Jsonb(json.loads(json.dumps(payload, default=str))), as_of),
        )

    def _ensure_position(
        self,
        conn: psycopg.Connection,
        user_id: str,
        generation: int,
        symbol: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO paper_positions (user_id, generation, symbol)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (user_id, generation, symbol),
        )
        row = conn.execute(
            f"""
            SELECT * FROM paper_positions
            WHERE user_id = %s AND generation = %s AND symbol = %s{' FOR UPDATE' if for_update else ''}
            """,
            (user_id, generation, symbol),
        ).fetchone()
        return dict(row)

    def _release_reservation(
        self,
        conn: psycopg.Connection,
        order: dict[str, Any],
        run: dict[str, Any],
    ) -> Decimal:
        if order["side"] == "buy":
            released = order["qty"] * order["limit_price"]
            conn.execute(
                """
                UPDATE paper_account_runs SET reserved_cash = reserved_cash - %s
                WHERE user_id = %s AND generation = %s
                """,
                (released, order["user_id"], order["generation"]),
            )
            return -released
        self._ensure_position(
            conn,
            order["user_id"],
            order["generation"],
            order["symbol"],
            for_update=True,
        )
        conn.execute(
            """
            UPDATE paper_positions SET reserved_qty = reserved_qty - %s, updated_at = now()
            WHERE user_id = %s AND generation = %s AND symbol = %s
            """,
            (order["qty"], order["user_id"], order["generation"], order["symbol"]),
        )
        return Decimal("0")

    def _append_event(
        self,
        conn: psycopg.Connection,
        order: dict[str, Any],
        event_type: str,
        status: str,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO paper_order_events (
                event_id, order_id, user_id, generation, event_type, status, reason, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"paper_evt_{uuid4().hex}",
                order["order_id"],
                order["user_id"],
                order["generation"],
                event_type,
                status,
                reason,
                Jsonb(payload or {}),
            ),
        )

    def _append_ledger(
        self,
        conn: psycopg.Connection,
        user_id: str,
        generation: int,
        run: dict[str, Any],
        event_type: str,
        *,
        order_id: str | None = None,
        cash_delta: Decimal = Decimal("0"),
        reserved_cash_delta: Decimal = Decimal("0"),
    ) -> None:
        conn.execute(
            """
            INSERT INTO paper_cash_ledger (
                entry_id, user_id, generation, order_id, event_type,
                cash_delta, reserved_cash_delta, cash_balance_after, reserved_cash_after
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"paper_led_{uuid4().hex}",
                user_id,
                generation,
                order_id,
                event_type,
                cash_delta,
                reserved_cash_delta,
                run["cash_balance"],
                run["reserved_cash"],
            ),
        )
