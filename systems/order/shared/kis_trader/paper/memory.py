from __future__ import annotations

from decimal import Decimal
from threading import RLock
from typing import Any
from uuid import uuid4

from kis_trader.domain.commands import OrderRequest

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
    utc_now,
)


class InMemoryPaperTradingRepository:
    """Thread-safe test/local implementation of the paper-trading contract."""

    def __init__(
        self,
        *,
        default_starting_cash: Decimal = DEFAULT_STARTING_CASH,
        max_active_order_symbols: int = MAX_ACTIVE_ORDER_SYMBOLS,
    ) -> None:
        self.default_starting_cash = Decimal(default_starting_cash)
        self.max_active_order_symbols = max_active_order_symbols
        self.accounts: dict[str, dict[str, Any]] = {}
        self.runs: dict[tuple[str, int], dict[str, Any]] = {}
        self.positions: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self.ledger: list[dict[str, Any]] = []
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
            open_orders = self.list_orders(user_id, status=PENDING_STATUS, limit=500)
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
            if request.symbol not in active_symbols and len(active_symbols) >= self.max_active_order_symbols:
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
            if row["status"] != PENDING_STATUS:
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
            account, run = self._current(user_id)
            old_generation = account["current_generation"]
            for row in list(self.orders.values()):
                if row["user_id"] != user_id or row["generation"] != old_generation or row["status"] != PENDING_STATUS:
                    continue
                self._release_reservation(row, run)
                now = utc_now()
                row.update(status=CANCELLED_STATUS, reason="account_reset", cancelled_at=now, updated_at=now)
                self._append_event(row, "order.cancelled", CANCELLED_STATUS, "account_reset")
            run.update(status="archived", reserved_cash=Decimal("0"), ended_at=utc_now())
            next_generation = old_generation + 1
            account["current_generation"] = next_generation
            account["updated_at"] = utc_now()
            self.runs[(user_id, next_generation)] = self._new_run(starting_cash)
            self._append_ledger(
                user_id,
                next_generation,
                self.runs[(user_id, next_generation)],
                "account.reset",
                cash_delta=starting_cash,
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
    ) -> list[dict[str, Any]]:
        symbol = symbol.upper()
        bid = Decimal(bid_price) if bid_price is not None else None
        ask = Decimal(ask_price) if ask_price is not None else None
        matched: list[dict[str, Any]] = []
        with self._lock:
            pending = sorted(
                (row for row in self.orders.values() if row["symbol"] == symbol and row["status"] == PENDING_STATUS),
                key=lambda row: (row["created_at"], row["order_id"]),
            )
            for row in pending:
                fill_price = ask if row["side"] == "buy" and ask is not None and ask <= row["limit_price"] else None
                if row["side"] == "sell" and bid is not None and bid >= row["limit_price"]:
                    fill_price = bid
                if fill_price is None or fill_price <= 0:
                    continue
                _account, run = self._current_run_for_order(row)
                position = self._position(row["user_id"], row["generation"], row["symbol"])
                qty = row["qty"]
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
                row.update(
                    status=FILLED_STATUS,
                    filled_qty=qty,
                    fill_price=fill_price,
                    quote_event_id=quote_event_id,
                    quote_timestamp=quote_timestamp,
                    reason=None,
                    filled_at=now,
                    updated_at=now,
                )
                self._append_event(row, "order.filled", FILLED_STATUS, payload={
                    "fill_price": str(fill_price),
                    "quote_event_id": quote_event_id,
                    "quote_timestamp": quote_timestamp,
                })
                self._append_ledger(
                    row["user_id"],
                    row["generation"],
                    run,
                    "order.filled",
                    order_id=row["order_id"],
                    cash_delta=cash_delta,
                    reserved_cash_delta=reserved_delta,
                )
                matched.append(public_order(row))
        return matched

    def active_order_symbols(self) -> list[str]:
        with self._lock:
            return sorted({row["symbol"] for row in self.orders.values() if row["status"] == PENDING_STATUS})

    def active_position_symbols(self) -> list[str]:
        with self._lock:
            return sorted({row["symbol"] for row in self.positions.values() if row["qty"] > 0})

    def _ensure_account(self, user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        account = self.accounts.get(user_id)
        if account is None:
            now = utc_now()
            account = {
                "user_id": user_id,
                "current_generation": 1,
                "currency": "USD",
                "created_at": now,
                "updated_at": now,
            }
            run = self._new_run(self.default_starting_cash)
            self.accounts[user_id] = account
            self.runs[(user_id, 1)] = run
            self._append_ledger(user_id, 1, run, "account.opened", cash_delta=self.default_starting_cash)
        return account, self.runs[(user_id, account["current_generation"])]

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
        if order["side"] == "buy":
            released = order["qty"] * order["limit_price"]
            run["reserved_cash"] -= released
            return -released
        position = self._position(order["user_id"], order["generation"], order["symbol"])
        position["reserved_qty"] -= order["qty"]
        position["updated_at"] = utc_now()
        return Decimal("0")

    def _append_event(
        self,
        order: dict[str, Any],
        event_type: str,
        status: str,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.setdefault(order["order_id"], []).append({
            "event_id": f"paper_evt_{uuid4().hex}",
            "order_id": order["order_id"],
            "user_id": order["user_id"],
            "generation": order["generation"],
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
        cash_delta: Decimal = Decimal("0"),
        reserved_cash_delta: Decimal = Decimal("0"),
    ) -> None:
        self.ledger.append({
            "entry_id": f"paper_led_{uuid4().hex}",
            "user_id": user_id,
            "generation": generation,
            "order_id": order_id,
            "event_type": event_type,
            "cash_delta": cash_delta,
            "reserved_cash_delta": reserved_cash_delta,
            "cash_balance_after": run["cash_balance"],
            "reserved_cash_after": run["reserved_cash"],
            "created_at": utc_now(),
        })
