from __future__ import annotations

from decimal import Decimal
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from kis_trader.domain.commands import OrderRequest

from .fixture import (
    DEMO_FILLS,
    DEMO_FINAL_CASH,
    DEMO_HOLDINGS,
    DEMO_PENDING_ORDERS,
    HOLDING_BY_SYMBOL,
    DEMO_REALIZED_PNL,
    SEED_PROFILE,
    fallback_price,
    seed_snapshot_history,
)

from .models import (
    ACTIVE_STATUSES,
    CANCELLED_STATUS,
    DEFAULT_STARTING_CASH,
    FILLED_STATUS,
    MAX_ACTIVE_ORDER_SYMBOLS,
    PARTIALLY_FILLED_STATUS,
    PENDING_STATUS,
    PaperCapacityError,
    PaperIdempotencyConflictError,
    PaperOrderCreationResult,
    PaperOrderError,
    PaperOrderNotFoundError,
    public_order,
    utc_now,
)


class InMemoryPaperTradingRepository:
    """Thread-safe test/local implementation of the paper-trading contract."""

    def __init__(
        self,
        *,
        default_starting_cash: Decimal = DEFAULT_STARTING_CASH,
        max_active_order_symbols: int = MAX_ACTIVE_ORDER_SYMBOLS,
        seed_profile: str | None = None,
    ) -> None:
        self.default_starting_cash = Decimal(default_starting_cash)
        self.max_active_order_symbols = max_active_order_symbols
        self.seed_profile = seed_profile
        self.accounts: dict[str, dict[str, Any]] = {}
        self.runs: dict[tuple[str, int], dict[str, Any]] = {}
        self.positions: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.portfolio_history: list[dict[str, Any]] = []
        self._lock = RLock()

    def ensure_account(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_account(user_id)
            return self.account_snapshot(user_id)

    def account_snapshot(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            account, run = self._current(user_id)
            positions = [
                dict(row)
                for (owner, generation, _symbol), row in self.positions.items()
                if owner == user_id and generation == account["current_generation"] and row["qty"] > 0
            ]
            positions.sort(key=lambda row: row["symbol"])
            realized_pnl = sum(
                (Decimal(row["realized_pnl"]) for (owner, generation, _symbol), row in self.positions.items()
                 if owner == user_id and generation == account["current_generation"]),
                Decimal("0"),
            )
            open_orders = [
                row for row in self.list_orders(user_id, limit=500)
                if row["status"] in ACTIVE_STATUSES
            ]
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
                    "realized_pnl": realized_pnl,
                    "started_at": run["started_at"],
                    "seed_profile": account.get("seed_profile"),
                    "seeded_at": account.get("seeded_at"),
                    "seed_suppressed_at": account.get("seed_suppressed_at"),
                },
                "positions": positions,
                "open_orders": open_orders,
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
        with self._lock:
            account, run = self._current(user_id)
            idempotency_key = (user_id, idempotency_key_hash)
            existing = self.idempotency.get(idempotency_key)
            if existing is not None:
                existing_body_hash, order_id = existing
                if existing_body_hash != body_hash:
                    raise PaperIdempotencyConflictError("idempotency key reused with a different request body")
                return PaperOrderCreationResult(public_order(self.orders[order_id]), True)

            active_symbols = set(self.active_order_symbols())
            if execution_mode == "paper" and request.symbol not in active_symbols and len(active_symbols) >= self.max_active_order_symbols:
                raise PaperCapacityError(
                    f"paper order realtime subscription limit reached ({self.max_active_order_symbols} symbols)"
                )

            position = self._position(user_id, account["current_generation"], request.symbol)
            if request.side == "buy":
                required = request.qty * request.price
                available = run["cash_balance"] - run["reserved_cash"]
                if required > available:
                    raise PaperOrderError("insufficient paper buying power")
                run["reserved_cash"] += required
                reserved_cash_delta = required
            else:
                available_qty = position["qty"] - position["reserved_qty"]
                if request.qty > available_qty:
                    raise PaperOrderError("insufficient paper position quantity")
                position["reserved_qty"] += request.qty
                position["updated_at"] = utc_now()
                reserved_cash_delta = Decimal("0")

            now = utc_now()
            order_id = f"paper_ord_{uuid4().hex}"
            row = {
                "order_id": order_id,
                "user_id": user_id,
                "generation": account["current_generation"],
                "market": request.market,
                "symbol": request.symbol,
                "side": request.side,
                "qty": request.qty,
                "limit_price": request.price,
                "exchange": request.exchange,
                "order_division": request.order_division,
                "order_type": order_type,
                "execution_mode": execution_mode,
                "simulation_run_id": simulation_run_id,
                "simulation_submitted_sequence": simulation_submitted_sequence,
                "virtual_submitted_at": virtual_submitted_at,
                "virtual_filled_at": None,
                "seed_profile": None,
                "status": PENDING_STATUS,
                "filled_qty": Decimal("0"),
                "fill_price": None,
                "quote_event_id": None,
                "quote_timestamp": None,
                "reason": None,
                "idempotency_key_hash": idempotency_key_hash,
                "body_hash": body_hash,
                "created_at": now,
                "updated_at": now,
                "filled_at": None,
                "cancelled_at": None,
            }
            self.orders[order_id] = row
            self.idempotency[idempotency_key] = (body_hash, order_id)
            self._append_event(row, "order.created", PENDING_STATUS)
            self._append_ledger(
                user_id,
                account["current_generation"],
                run,
                "order.reserved",
                order_id=order_id,
                reserved_cash_delta=reserved_cash_delta,
            )
            return PaperOrderCreationResult(public_order(row), False)

    def pretrade(self, user_id: str, request: OrderRequest) -> dict[str, Any]:
        with self._lock:
            account, run = self._current(user_id)
            available_cash = run["cash_balance"] - run["reserved_cash"]
            position = self._position(user_id, account["current_generation"], request.symbol)
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
        with self._lock:
            row = self.orders.get(order_id)
            return public_order(row) if row and row["user_id"] == user_id else None

    def list_orders(
        self,
        user_id: str,
        *,
        status: str | None = None,
        include_previous: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            account, _run = self._current(user_id)
            rows = [
                row for row in self.orders.values()
                if row["user_id"] == user_id
                and (include_previous or row["generation"] == account["current_generation"])
                and (status is None or row["status"] == status)
            ]
            rows.sort(key=lambda row: (row["created_at"], row["order_id"]), reverse=True)
            return [public_order(row) for row in rows[:max(1, min(limit, 500))]]

    def list_order_events(self, user_id: str, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            row = self.orders.get(order_id)
            if not row or row["user_id"] != user_id:
                return []
            return [dict(event) for event in self.events.get(order_id, [])]

    def cancel_order(self, user_id: str, order_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.orders.get(order_id)
            if not row or row["user_id"] != user_id:
                raise PaperOrderNotFoundError("paper order not found")
            if row["status"] not in ACTIVE_STATUSES:
                return public_order(row)
            _account, run = self._current_run_for_order(row)
            reserved_delta = self._release_reservation(row, run)
            now = utc_now()
            row.update(status=CANCELLED_STATUS, reason="cancelled_by_user", cancelled_at=now, updated_at=now)
            self._append_event(row, "order.cancelled", CANCELLED_STATUS, "cancelled_by_user")
            self._append_ledger(
                user_id,
                row["generation"],
                run,
                "order.reservation_released",
                order_id=order_id,
                reserved_cash_delta=reserved_delta,
            )
            return public_order(row)

    def reset_account(self, user_id: str, starting_cash: Decimal) -> dict[str, Any]:
        starting_cash = Decimal(starting_cash)
        if starting_cash <= 0:
            raise PaperOrderError("starting_cash must be positive")
        with self._lock:
            account, run = self._ensure_account(user_id, apply_seed=False)
            self._rotate_account(
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
        bid_size: Decimal | None = None,
        ask_size: Decimal | None = None,
        execution_mode: str = "paper",
        simulation_run_id: str | None = None,
        quote_sequence: int | None = None,
        virtual_timestamp: str | None = None,
    ) -> list[dict[str, Any]]:
        symbol = symbol.upper()
        bid = Decimal(bid_price) if bid_price is not None else None
        ask = Decimal(ask_price) if ask_price is not None else None
        bid_liquidity = Decimal(bid_size) if bid_size is not None else None
        ask_liquidity = Decimal(ask_size) if ask_size is not None else None
        matched: list[dict[str, Any]] = []
        with self._lock:
            pending = sorted(
                (
                    row for row in self.orders.values()
                    if row["symbol"] == symbol
                    and row["status"] in ACTIVE_STATUSES
                    and row.get("execution_mode", "paper") == execution_mode
                    and (execution_mode != "simulation" or row.get("simulation_run_id") == simulation_run_id)
                    and (
                        execution_mode != "simulation"
                        or quote_sequence is None
                        or row.get("simulation_submitted_sequence") is None
                        or (
                            int(row["simulation_submitted_sequence"]) <= int(quote_sequence)
                            if row.get("order_type") == "market"
                            else int(row["simulation_submitted_sequence"]) < int(quote_sequence)
                        )
                    )
                ),
                key=lambda row: (row["created_at"], row["order_id"]),
            )
            for row in pending:
                fill_price = ask if row["side"] == "buy" and ask is not None and ask <= row["limit_price"] else None
                if row["side"] == "sell" and bid is not None and bid >= row["limit_price"]:
                    fill_price = bid
                if fill_price is None or fill_price <= 0:
                    continue
                remaining_qty = row["qty"] - row["filled_qty"]
                available_qty = ask_liquidity if row["side"] == "buy" else bid_liquidity
                execution_qty = min(remaining_qty, available_qty) if available_qty is not None else remaining_qty
                if execution_qty <= 0:
                    continue
                execution_id = self._record_execution(
                    row,
                    quantity=execution_qty,
                    price=fill_price,
                    quote_event_id=quote_event_id,
                    quote_timestamp=quote_timestamp,
                    executed_at=virtual_timestamp or quote_timestamp,
                )
                if execution_id is None:
                    continue
                if row["side"] == "buy" and ask_liquidity is not None:
                    ask_liquidity -= execution_qty
                if row["side"] == "sell" and bid_liquidity is not None:
                    bid_liquidity -= execution_qty
                _account, run = self._current_run_for_order(row)
                position = self._position(row["user_id"], row["generation"], row["symbol"])
                qty = execution_qty
                if row["side"] == "buy":
                    reserved = qty * row["limit_price"]
                    cost = qty * fill_price
                    old_qty = position["qty"]
                    position["average_price"] = (
                        ((old_qty * position["average_price"]) + cost) / (old_qty + qty)
                    )
                    position["qty"] += qty
                    run["cash_balance"] -= cost
                    run["reserved_cash"] -= reserved
                    cash_delta = -cost
                    reserved_delta = -reserved
                else:
                    proceeds = qty * fill_price
                    position["qty"] -= qty
                    position["reserved_qty"] -= qty
                    position["realized_pnl"] += (fill_price - position["average_price"]) * qty
                    run["cash_balance"] += proceeds
                    cash_delta = proceeds
                    reserved_delta = Decimal("0")
                position["updated_at"] = utc_now()
                now = utc_now()
                previous_filled_qty = row["filled_qty"]
                next_filled_qty = previous_filled_qty + qty
                previous_average = row.get("fill_price") or Decimal("0")
                next_fill_average = (
                    (previous_filled_qty * previous_average) + (qty * fill_price)
                ) / next_filled_qty
                next_status = FILLED_STATUS if next_filled_qty == row["qty"] else PARTIALLY_FILLED_STATUS
                event_type = "order.filled" if next_status == FILLED_STATUS else "order.partially_filled"
                row.update(
                    status=next_status,
                    filled_qty=next_filled_qty,
                    fill_price=next_fill_average,
                    quote_event_id=quote_event_id,
                    quote_timestamp=quote_timestamp,
                    reason=None,
                    filled_at=now if next_status == FILLED_STATUS else None,
                    updated_at=now,
                    virtual_filled_at=(
                        virtual_timestamp
                        if execution_mode == "simulation" and next_status == FILLED_STATUS
                        else None
                    ),
                )
                self._append_event(row, event_type, next_status, payload={
                    "execution_id": execution_id,
                    "execution_quantity": str(qty),
                    "fill_price": str(fill_price),
                    "quote_event_id": quote_event_id,
                    "quote_timestamp": quote_timestamp,
                }, execution_id=execution_id)
                self._append_ledger(
                    row["user_id"],
                    row["generation"],
                    run,
                    event_type,
                    order_id=row["order_id"],
                    execution_id=execution_id,
                    cash_delta=cash_delta,
                    reserved_cash_delta=reserved_delta,
                )
                self._append_portfolio_snapshot(row["user_id"], row["generation"], virtual_timestamp or quote_timestamp, {symbol: fill_price})
                matched.append(public_order(row))
        return matched

    def cancel_simulation_run(self, run_id: str, *, reason: str = "simulation_run_ended") -> list[dict[str, Any]]:
        cancelled: list[dict[str, Any]] = []
        with self._lock:
            rows = [
                row for row in self.orders.values()
                if row.get("execution_mode") == "simulation"
                and row.get("simulation_run_id") == run_id
                and row["status"] in ACTIVE_STATUSES
            ]
            for row in rows:
                _account, run = self._current_run_for_order(row)
                reserved_delta = self._release_reservation(row, run)
                now = utc_now()
                row.update(status=CANCELLED_STATUS, reason=reason, cancelled_at=now, updated_at=now)
                self._append_event(row, "order.cancelled", CANCELLED_STATUS, reason)
                self._append_ledger(
                    row["user_id"], row["generation"], run, "order.reservation_released",
                    order_id=row["order_id"], reserved_cash_delta=reserved_delta,
                )
                cancelled.append(public_order(row))
        return cancelled

    def active_order_symbols(self) -> list[str]:
        with self._lock:
            return sorted({
                row["symbol"] for row in self.orders.values()
                if row["status"] in ACTIVE_STATUSES and row.get("execution_mode", "paper") == "paper"
            })

    def active_position_symbols(self) -> list[str]:
        with self._lock:
            return sorted({row["symbol"] for row in self.positions.values() if row["qty"] > 0})

    def _ensure_account(
        self,
        user_id: str,
        *,
        apply_seed: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        account = self.accounts.get(user_id)
        if account is None:
            now = utc_now()
            account = {
                "user_id": user_id,
                "current_generation": 1,
                "currency": "USD",
                "created_at": now,
                "updated_at": now,
                "seed_profile": None,
                "seeded_at": None,
                "seed_suppressed_at": None,
            }
            run = self._new_run(self.default_starting_cash)
            self.accounts[user_id] = account
            self.runs[(user_id, 1)] = run
            self._append_ledger(user_id, 1, run, "account.opened", cash_delta=self.default_starting_cash)
        if apply_seed:
            self._maybe_seed_account(account, self.runs[(user_id, account["current_generation"])])
        return account, self.runs[(user_id, account["current_generation"])]

    def _maybe_seed_account(self, account: dict[str, Any], run: dict[str, Any]) -> None:
        if self.seed_profile != SEED_PROFILE:
            return
        if account.get("seed_suppressed_at") or account.get("seed_profile") == SEED_PROFILE:
            return
        user_id = account["user_id"]
        generation = account["current_generation"]
        has_orders = any(
            row["user_id"] == user_id and row["generation"] == generation
            for row in self.orders.values()
        )
        has_positions = any(
            owner == user_id and row_generation == generation and row["qty"] > 0
            for (owner, row_generation, _symbol), row in self.positions.items()
        )
        if (
            has_orders or has_positions
            or run["cash_balance"] != self.default_starting_cash
            or run["reserved_cash"] != 0
        ):
            account, run = self._rotate_account(
                user_id,
                self.default_starting_cash,
                suppress_seed=False,
                current=(account, run),
            )
            generation = account["current_generation"]
        run["cash_balance"] = DEMO_FINAL_CASH
        now = utc_now()
        account["seed_profile"] = SEED_PROFILE
        account["seeded_at"] = now
        account["seed_suppressed_at"] = None
        realized_by_symbol = {
            "GOOGL": Decimal("211.20"), "XOM": Decimal("127.50"),
            "HD": Decimal("-84.00"), "AMZN": Decimal("50.00"),
        }
        for holding in DEMO_HOLDINGS:
            position = self._position(user_id, generation, holding.symbol)
            position.update(
                qty=holding.quantity,
                reserved_qty=Decimal("0"),
                average_price=holding.average_price,
                realized_pnl=realized_by_symbol.get(holding.symbol, Decimal("0")),
                updated_at=now,
            )
        seed_cash = self.default_starting_cash
        for index, fill in enumerate(DEMO_FILLS, start=1):
            seed_key = f"{SEED_PROFILE}:{user_id}:{generation}:{index}"
            order_id = f"paper_seed_{uuid5(NAMESPACE_URL, seed_key).hex}"
            row = {
                "order_id": order_id, "user_id": user_id, "generation": generation,
                "market": "overseas", "symbol": fill.symbol, "side": fill.side,
                "qty": fill.quantity, "limit_price": fill.price, "exchange": HOLDING_BY_SYMBOL[fill.symbol].exchange,
                "order_division": "00", "order_type": "limit", "execution_mode": "paper",
                "simulation_run_id": None, "simulation_submitted_sequence": None,
                "virtual_submitted_at": None, "virtual_filled_at": None,
                "seed_profile": SEED_PROFILE, "status": FILLED_STATUS, "filled_qty": fill.quantity,
                "fill_price": fill.price, "quote_event_id": f"seed:{SEED_PROFILE}:{index}",
                "quote_timestamp": fill.filled_at.isoformat(), "reason": None,
                "idempotency_key_hash": f"seed:{SEED_PROFILE}:{generation}:{index}", "body_hash": SEED_PROFILE,
                "created_at": fill.filled_at, "updated_at": fill.filled_at,
                "filled_at": fill.filled_at, "cancelled_at": None,
            }
            self.orders[order_id] = row
            self.idempotency[(user_id, row["idempotency_key_hash"])] = (SEED_PROFILE, order_id)
            execution_id = self._record_execution(
                row,
                quantity=fill.quantity,
                price=fill.price,
                quote_event_id=f"seed:{SEED_PROFILE}:{generation}:{index}",
                quote_timestamp=fill.filled_at.isoformat(),
                executed_at=fill.filled_at.isoformat(),
            )
            self._append_event(
                row,
                "order.filled",
                FILLED_STATUS,
                payload={"seed_profile": SEED_PROFILE, "execution_id": execution_id},
                execution_id=execution_id,
            )
            cash_delta = fill.quantity * fill.price * (Decimal("-1") if fill.side == "buy" else Decimal("1"))
            seed_cash += cash_delta
            self.ledger.append({
                "entry_id": f"paper_seed_led_{uuid5(NAMESPACE_URL, f'{seed_key}:ledger').hex}",
                "user_id": user_id,
                "generation": generation,
                "order_id": order_id,
                "execution_id": execution_id,
                "event_type": "order.filled",
                "cash_delta": cash_delta,
                "reserved_cash_delta": Decimal("0"),
                "cash_balance_after": seed_cash,
                "reserved_cash_after": Decimal("0"),
                "created_at": fill.filled_at,
            })
        for index, pending in enumerate(DEMO_PENDING_ORDERS, start=1):
            seed_key = f"{SEED_PROFILE}:{user_id}:{generation}:pending:{index}"
            order_id = f"paper_seed_{uuid5(NAMESPACE_URL, seed_key).hex}"
            row = {
                "order_id": order_id, "user_id": user_id, "generation": generation,
                "market": "overseas", "symbol": pending.symbol, "side": pending.side,
                "qty": pending.quantity, "limit_price": pending.limit_price,
                "exchange": HOLDING_BY_SYMBOL[pending.symbol].exchange,
                "order_division": "00", "order_type": "limit", "execution_mode": "paper",
                "simulation_run_id": None, "simulation_submitted_sequence": None,
                "virtual_submitted_at": None, "virtual_filled_at": None,
                "seed_profile": SEED_PROFILE, "status": PENDING_STATUS, "filled_qty": Decimal("0"),
                "fill_price": None, "quote_event_id": None, "quote_timestamp": None, "reason": None,
                "idempotency_key_hash": f"seed:{SEED_PROFILE}:{generation}:pending:{index}",
                "body_hash": SEED_PROFILE, "created_at": pending.created_at, "updated_at": pending.created_at,
                "filled_at": None, "cancelled_at": None,
            }
            reserved_cash_delta = Decimal("0")
            if pending.side == "buy":
                reserved_cash_delta = pending.quantity * pending.limit_price
                run["reserved_cash"] += reserved_cash_delta
            else:
                position = self._position(user_id, generation, pending.symbol)
                position["reserved_qty"] += pending.quantity
                position["updated_at"] = pending.created_at
            self.orders[order_id] = row
            self.idempotency[(user_id, row["idempotency_key_hash"])] = (SEED_PROFILE, order_id)
            self._append_event(row, "order.created", PENDING_STATUS, payload={"seed_profile": SEED_PROFILE})
            self._append_ledger(
                user_id, generation, run, "order.reserved",
                order_id=order_id, reserved_cash_delta=reserved_cash_delta,
            )
        self.portfolio_history.extend(
            {"user_sub": user_id, "source_as_of": source_as_of, "payload": snapshot}
            for source_as_of, snapshot in seed_snapshot_history()
        )
        self._append_ledger(
            user_id, generation, run, "account.seeded",
            cash_delta=Decimal("0"),
        )
        if seed_cash != DEMO_FINAL_CASH:
            raise AssertionError("demo fixture cash invariant failed")
        if sum(
            (
                row["realized_pnl"]
                for (owner, row_generation, _symbol), row in self.positions.items()
                if owner == user_id and row_generation == generation
            ),
            Decimal("0"),
        ) != DEMO_REALIZED_PNL:
            raise AssertionError("demo fixture realized P&L invariant failed")

    def _rotate_account(
        self,
        user_id: str,
        starting_cash: Decimal,
        *,
        suppress_seed: bool,
        current: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        account, run = current or self._current(user_id)
        old_generation = account["current_generation"]
        reason = "account_reset" if suppress_seed else "account_demo_profile"
        for row in list(self.orders.values()):
            if row["user_id"] != user_id or row["generation"] != old_generation or row["status"] not in ACTIVE_STATUSES:
                continue
            self._release_reservation(row, run)
            now = utc_now()
            row.update(status=CANCELLED_STATUS, reason=reason, cancelled_at=now, updated_at=now)
            self._append_event(row, "order.cancelled", CANCELLED_STATUS, reason)
        run.update(status="archived", reserved_cash=Decimal("0"), ended_at=utc_now())
        next_generation = old_generation + 1
        account["current_generation"] = next_generation
        account["updated_at"] = utc_now()
        account["seed_suppressed_at"] = utc_now() if suppress_seed else None
        account["seed_profile"] = None
        account["seeded_at"] = None
        next_run = self._new_run(starting_cash)
        self.runs[(user_id, next_generation)] = next_run
        self._append_ledger(
            user_id,
            next_generation,
            next_run,
            "account.reset" if suppress_seed else "account.seed_profile_upgraded",
            cash_delta=starting_cash,
        )
        return account, next_run

    def _current(self, user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._ensure_account(user_id)

    def _current_run_for_order(self, order: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        account = self.accounts[order["user_id"]]
        return account, self.runs[(order["user_id"], order["generation"])]

    def _new_run(self, starting_cash: Decimal) -> dict[str, Any]:
        return {
            "starting_cash": Decimal(starting_cash),
            "cash_balance": Decimal(starting_cash),
            "reserved_cash": Decimal("0"),
            "status": "active",
            "started_at": utc_now(),
            "ended_at": None,
        }

    def _append_portfolio_snapshot(
        self,
        user_id: str,
        generation: int,
        as_of: str | None,
        price_overrides: dict[str, Decimal],
    ) -> None:
        run = self.runs[(user_id, generation)]
        rendered = []
        market_value = Decimal("0")
        holdings_cost = Decimal("0")
        for (owner, row_generation, symbol), row in sorted(self.positions.items()):
            if owner != user_id or row_generation != generation or row["qty"] <= 0:
                continue
            price = price_overrides.get(symbol) or fallback_price(symbol) or row["average_price"]
            value = row["qty"] * price
            cost = row["qty"] * row["average_price"]
            market_value += value
            holdings_cost += cost
            rendered.append({
                "symbol": symbol,
                "quantity": row["qty"],
                "averagePrice": row["average_price"],
                "currentPrice": price,
                "marketValueForeign": value,
                "purchaseAmountForeign": cost,
            })
        equity = run["cash_balance"] + market_value
        payload = {
            "asOf": as_of or utc_now().isoformat(),
            "source": "account-history",
            "account": {
                "cashForeign": run["cash_balance"],
                "stockValueForeign": market_value,
                "totalValueForeign": equity,
                "netInvestedPrincipal": run["starting_cash"],
                "unrealizedPnlForeign": market_value - holdings_cost,
            },
            "positions": rendered,
        }
        self.portfolio_history.append({"user_sub": user_id, "source_as_of": payload["asOf"], "payload": payload})

    def _position(self, user_id: str, generation: int, symbol: str) -> dict[str, Any]:
        key = (user_id, generation, symbol)
        if key not in self.positions:
            self.positions[key] = {
                "user_id": user_id,
                "generation": generation,
                "symbol": symbol,
                "qty": Decimal("0"),
                "reserved_qty": Decimal("0"),
                "average_price": Decimal("0"),
                "realized_pnl": Decimal("0"),
                "updated_at": utc_now(),
            }
        return self.positions[key]

    def _release_reservation(self, order: dict[str, Any], run: dict[str, Any]) -> Decimal:
        remaining_qty = order["qty"] - (order["filled_qty"] or Decimal("0"))
        if order["side"] == "buy":
            released = remaining_qty * order["limit_price"]
            run["reserved_cash"] -= released
            return -released
        position = self._position(order["user_id"], order["generation"], order["symbol"])
        position["reserved_qty"] -= remaining_qty
        position["updated_at"] = utc_now()
        return Decimal("0")

    def _append_event(
        self,
        order: dict[str, Any],
        event_type: str,
        status: str,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
        execution_id: str | None = None,
    ) -> None:
        self.events.setdefault(order["order_id"], []).append({
            "event_id": f"paper_evt_{uuid4().hex}",
            "order_id": order["order_id"],
            "user_id": order["user_id"],
            "generation": order["generation"],
            "execution_id": execution_id,
            "event_type": event_type,
            "status": status,
            "reason": reason,
            "payload": payload or {},
            "created_at": utc_now(),
        })

    def _append_ledger(
        self,
        user_id: str,
        generation: int,
        run: dict[str, Any],
        event_type: str,
        *,
        order_id: str | None = None,
        execution_id: str | None = None,
        cash_delta: Decimal = Decimal("0"),
        reserved_cash_delta: Decimal = Decimal("0"),
    ) -> None:
        self.ledger.append({
            "entry_id": f"paper_led_{uuid4().hex}",
            "user_id": user_id,
            "generation": generation,
            "order_id": order_id,
            "execution_id": execution_id,
            "event_type": event_type,
            "cash_delta": cash_delta,
            "reserved_cash_delta": reserved_cash_delta,
            "cash_balance_after": run["cash_balance"],
            "reserved_cash_after": run["reserved_cash"],
            "created_at": utc_now(),
        })

    def _record_execution(
        self,
        order: dict[str, Any],
        *,
        quantity: Decimal,
        price: Decimal,
        quote_event_id: str | None,
        quote_timestamp: str | None,
        executed_at: str | None,
    ) -> str | None:
        if quote_event_id is not None and any(
            item["order_id"] == order["order_id"] and item.get("quote_event_id") == quote_event_id
            for item in self.executions.values()
        ):
            return None
        sequence = 1 + sum(1 for item in self.executions.values() if item["order_id"] == order["order_id"])
        execution_key = f"{order['order_id']}:{sequence}"
        execution_id = f"paper_exec_{uuid5(NAMESPACE_URL, execution_key).hex}"
        self.executions.setdefault(execution_id, {
            "execution_id": execution_id,
            "order_id": order["order_id"],
            "execution_sequence": sequence,
            "quantity": quantity,
            "price": price,
            "fee": Decimal("0"),
            "quote_event_id": quote_event_id,
            "quote_timestamp": quote_timestamp,
            "executed_at": executed_at or utc_now(),
        })
        return execution_id
