"""Deterministic virtual clock and quote-driven trading ledger."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol

from gops_simul.dataset import ALLOWED_SPEEDS, DATASET_END, DATASET_ID, DATASET_START, REPLAY_SYMBOLS, in_half_open_window, parse_timestamp
from gops_simul.state_store import ReplayStateStore


KST = timezone(timedelta(hours=9))
INITIAL_CASH = 100_000.0


@dataclass(frozen=True)
class ReplayEvent:
    sequence: int
    timestamp: datetime
    feed: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        if not in_half_open_window(self.timestamp, DATASET_START, DATASET_END):
            raise ValueError("event is outside the replay dataset window")
        if str(self.payload.get("S") or "").upper() not in REPLAY_SYMBOLS:
            raise ValueError("unsupported replay symbol")


class ReplayEventSource(Protocol):
    dataset_id: str
    total_events: int
    def events_after(self, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]: ...
    def candle_snapshot(self, symbol: str, interval: str, through: datetime, limit: int) -> dict[str, object]: ...


class InMemoryReplayEventSource:
    dataset_id = DATASET_ID

    def __init__(self, events: Iterable[ReplayEvent]) -> None:
        self._events = sorted(events, key=lambda event: (event.timestamp, event.sequence))
        sequences = [event.sequence for event in self._events]
        if len(sequences) != len(set(sequences)):
            raise ValueError("replay event sequences must be unique")
        self.total_events = len(self._events)

    def events_after(self, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]:
        return [event for event in self._events if event.sequence > sequence and event.timestamp <= through][:limit]

    def candle_snapshot(self, symbol: str, interval: str, through: datetime, limit: int) -> dict[str, object]:
        from gops_simul.clickhouse import INTERVAL_SECONDS, replay_candle_payload
        normalized = symbol.strip().upper()
        if normalized not in REPLAY_SYMBOLS:
            raise ValueError(f"symbol is not available in {DATASET_ID}")
        seconds = INTERVAL_SECONDS.get(interval)
        if seconds is None:
            raise ValueError(f"unsupported replay candle interval: {interval}")
        buckets: dict[int, list[ReplayEvent]] = {}
        for event in self._events:
            if event.timestamp <= through and event.payload.get("T") == "t" and event.payload.get("S") == normalized:
                bucket = int(event.timestamp.timestamp()) // seconds * seconds
                buckets.setdefault(bucket, []).append(event)
        candles = []
        for bucket, events in sorted(buckets.items())[-limit:]:
            prices = [float(event.payload["p"]) for event in events]
            timestamp = datetime.fromtimestamp(bucket, tz=UTC)
            candles.append({
                "symbol": normalized, "interval": interval,
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1],
                "volume": sum(float(event.payload.get("s") or 0) for event in events),
                "tradeCount": len(events), "isClosed": timestamp + timedelta(seconds=seconds) <= through,
                "source": "simulation_replay", "feed": "mixed", "sourceInterval": "trades",
            })
        return replay_candle_payload(normalized, interval, candles, through)


class ReplayController:
    def __init__(self, source: ReplayEventSource, *, clock: Callable[[], float] = time.monotonic,
                 default_speed: int = 1, max_events_per_pump: int = 50_000,
                 state_store: ReplayStateStore | None = None) -> None:
        if default_speed not in ALLOWED_SPEEDS:
            raise ValueError(f"speed must be one of {ALLOWED_SPEEDS}")
        self.source = source
        self.clock = clock
        self.default_speed = default_speed
        self.max_events_per_pump = max(1, max_events_per_pump)
        self.state_store = state_store
        self._lock = threading.RLock()
        self.mode = "live"
        self.state = "idle"
        self.run_id: str | None = None
        self.requested_speed = default_speed
        self.cursor = DATASET_START
        self.last_sequence = 0
        self._anchor_wall = self.clock()
        self._anchor_virtual = DATASET_START
        self._run_started_wall: float | None = None
        self._emitted: deque[ReplayEvent] = deque(maxlen=100_000)
        self._latest_quotes: dict[str, dict[str, float]] = {}
        self._latest_trades: dict[str, float] = {}
        self._accounts: dict[str, dict[str, object]] = {}
        self._restore_state()

    def set_mode(self, mode: str) -> dict[str, object]:
        normalized = str(mode).strip().lower()
        if normalized not in {"live", "simulation"}:
            raise ValueError("mode must be live or simulation")
        with self._lock:
            if normalized == "simulation":
                self._new_run()
            else:
                old_run = self.run_id
                self.mode, self.state, self.run_id = "live", "idle", None
                self._accounts.clear()
                if old_run and self.state_store:
                    self.state_store.delete(old_run)
            return self._status()

    def resume(self) -> dict[str, object]:
        with self._lock:
            self._require_simulation()
            if self.state != "completed":
                self._reset_anchor()
                self.state = "running"
                if self._run_started_wall is None:
                    self._run_started_wall = self.clock()
                self._match_all_orders()
                self._persist()
            return self._status()

    def pause(self) -> dict[str, object]:
        with self._lock:
            self._require_simulation()
            self._pump()
            if self.state != "completed":
                self.state = "paused"
            self._reset_anchor()
            self._persist()
            return self._status()

    def restart(self) -> dict[str, object]:
        with self._lock:
            self._require_simulation()
            self._new_run()
            return self._status()

    def set_speed(self, speed: int) -> dict[str, object]:
        if speed not in ALLOWED_SPEEDS:
            raise ValueError(f"speed must be one of {ALLOWED_SPEEDS}")
        with self._lock:
            self._pump()
            self.requested_speed = speed
            self._reset_anchor()
            self._persist()
            return self._status()

    def status(self) -> dict[str, object]:
        with self._lock:
            self._pump()
            return self._status()

    def latest_quote(self, symbol: str) -> dict[str, float] | None:
        with self._lock:
            quote = self._latest_quotes.get(symbol.strip().upper())
            return dict(quote) if quote else None

    def emitted_events(self) -> list[ReplayEvent]:
        with self._lock:
            return list(self._emitted)

    def candle_snapshot(self, symbol: str, interval: str, limit: int = 2000) -> dict[str, object]:
        with self._lock:
            self._pump()
            return self.source.candle_snapshot(symbol, interval, self.cursor, limit)

    def account(self, user_id: str) -> dict[str, object]:
        with self._lock:
            account = self._account(user_id)
            positions = []
            for symbol, position in sorted(dict(account["positions"]).items()):
                quantity = int(position["quantity"])
                if quantity <= 0:
                    continue
                quote = self._latest_quotes.get(symbol)
                current = quote["bid"] if quote else float(position["averagePrice"])
                positions.append({"symbol": symbol, "quantity": quantity,
                    "averagePrice": round(float(position["averagePrice"]), 6), "currentPrice": round(current, 6),
                    "marketValueUsd": round(current * quantity, 6),
                    "reservedQuantity": int(position.get("reservedQuantity") or 0)})
            return {"status": "ok", "source": "gops-simulator", "simulation": True, "runId": self.run_id,
                "virtualTime": self._format(self.cursor),
                "account": {"alias": "틱 리플레이 · SIMULATED", "currency": "USD",
                    "cashForeign": round(float(account["cash"]), 6),
                    "reservedCash": round(float(account["reservedCash"]), 6),
                    "availableCash": round(float(account["cash"]) - float(account["reservedCash"]), 6)},
                "positions": positions, "orders": [dict(item) for item in account["orders"]],
                "conditions": [dict(item) for item in account["conditions"]],
                "limitations": ["whole-share full fills", "no short selling", "no fees"]}

    def submit_order(self, *, user_id: str, symbol: str, side: str, quantity: int,
                     order_type: str, idempotency_key: str, limit_price: float | None = None) -> dict[str, object]:
        with self._lock:
            result = self._submit_order(user_id=user_id, symbol=symbol, side=side, quantity=quantity,
                order_type=order_type, idempotency_key=idempotency_key, limit_price=limit_price)
            self._persist()
            return result

    def get_order(self, user_id: str, order_id: str) -> dict[str, object] | None:
        account = self._account(user_id)
        order = next((item for item in account["orders"] if item["order_id"] == order_id), None)
        return dict(order) if order else None

    def order_events(self, user_id: str, order_id: str) -> list[dict[str, object]]:
        order = self.get_order(user_id, order_id)
        if not order:
            return []
        events = [{"type": "order.accepted", "status": "accepted", "order_id": order_id,
            "simulation": True, "runId": self.run_id, "virtualTime": order["virtualSubmittedAt"]}]
        if order["status"] != "accepted":
            events.append({"type": f"order.{order['status']}", "status": order["status"], "order_id": order_id,
                "simulation": True, "runId": self.run_id,
                "virtualTime": order.get("virtualFilledAt") or order.get("virtualCanceledAt") or self._format(self.cursor)})
        return events

    def create_condition(self, user_id: str, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            symbol = self._symbol(payload.get("symbol"))
            direction = _one_of(payload.get("direction"), {"atOrAbove", "atOrBelow"}, "direction")
            trigger = _positive_float(payload.get("triggerPrice"), "triggerPrice")
            current = self._latest_trades.get(symbol)
            if current is None:
                raise ValueError("current replay trade is unavailable")
            if direction == "atOrAbove" and trigger <= current or direction == "atOrBelow" and trigger >= current:
                raise ValueError("triggerPrice must be beyond the current replay trade")
            account = self._account(user_id)
            condition_id = int(account["nextConditionId"])
            account["nextConditionId"] = condition_id + 1
            condition = {"id": condition_id, "runId": self.run_id, "symbol": symbol,
                "side": _one_of(payload.get("side"), {"buy", "sell"}, "side"), "direction": direction,
                "triggerPrice": trigger, "limitPrice": _positive_float(payload.get("limitPrice"), "limitPrice"),
                "quantity": _positive_int(payload.get("quantity"), "quantity"), "status": "watching",
                "executionEnabled": payload.get("executionEnabled") is not False,
                "alertsEnabled": payload.get("alertsEnabled") is not False,
                "validity": str(payload.get("validity") or "DAY"), "virtualCreatedAt": self._format(self.cursor)}
            account["conditions"].append(condition)
            self._persist()
            return dict(condition)

    def update_condition(self, user_id: str, condition_id: int, *, status: str | None = None,
                         alerts_enabled: bool | None = None) -> dict[str, object]:
        with self._lock:
            condition = self._condition(user_id, condition_id)
            if not condition:
                raise ValueError("trade condition not found")
            if status is not None:
                if status not in {"watching", "paused"}:
                    raise ValueError("status must be watching or paused")
                if condition["status"] not in {"watching", "paused"}:
                    raise ValueError("a triggered trade condition cannot be resumed")
                condition["status"] = status
            if alerts_enabled is not None:
                condition["alertsEnabled"] = bool(alerts_enabled)
            self._persist()
            return dict(condition)

    def delete_condition(self, user_id: str, condition_id: int) -> dict[str, object]:
        with self._lock:
            condition = self._condition(user_id, condition_id)
            if not condition:
                raise ValueError("trade condition not found")
            self._account(user_id)["conditions"].remove(condition)
            self._persist()
            return dict(condition)

    def _new_run(self) -> None:
        if self.run_id and self.state_store:
            self.state_store.delete(self.run_id)
        self.mode, self.state, self.run_id = "simulation", "ready", str(uuid.uuid4())
        self.requested_speed, self.cursor, self.last_sequence = self.default_speed, DATASET_START, 0
        self._emitted.clear(); self._latest_quotes.clear(); self._latest_trades.clear(); self._accounts.clear()
        self._run_started_wall = None
        self._reset_anchor(); self._persist()

    def _pump(self) -> None:
        if self.mode != "simulation" or self.state != "running":
            return
        now = self.clock()
        target = min(DATASET_END, self._anchor_virtual + timedelta(seconds=max(0.0, now - self._anchor_wall) * self.requested_speed))
        events = self.source.events_after(self.last_sequence, target, self.max_events_per_pump)
        for event in events:
            self._apply_event(event)
        if events:
            self.last_sequence = events[-1].sequence
        if len(events) >= self.max_events_per_pump:
            self.cursor = max(self.cursor, events[-1].timestamp)
            self._reset_anchor(now=now)
        else:
            self.cursor = target
        if self.cursor >= DATASET_END:
            self.cursor, self.state = DATASET_END, "completed"
            self._cancel_open()
        if events or self.state == "completed":
            self._persist()

    def _apply_event(self, event: ReplayEvent) -> None:
        self._emitted.append(event)
        payload, symbol = event.payload, str(event.payload.get("S") or "").upper()
        if payload.get("T") == "q":
            bid, ask = _nonnegative_float(payload.get("bp"), "bid"), _nonnegative_float(payload.get("ap"), "ask")
            if bid == 0 or ask == 0:
                self._latest_quotes.pop(symbol, None)
                return
            self._latest_quotes[symbol] = {"bid": bid, "ask": ask}
            self._match_orders(symbol)
        elif payload.get("T") == "t":
            price, previous = _positive_float(payload.get("p"), "price"), self._latest_trades.get(symbol)
            self._latest_trades[symbol] = price
            if previous is not None and previous != price:
                self._trigger_conditions(symbol, previous, price)

    def _submit_order(self, *, user_id: str, symbol: object, side: object, quantity: object,
                      order_type: object, idempotency_key: str, limit_price: object = None) -> dict[str, object]:
        self._require_simulation()
        symbol = self._symbol(symbol); side = _one_of(side, {"buy", "sell"}, "side")
        order_type = _one_of(order_type, {"market", "limit"}, "order_type")
        quantity = _positive_int(quantity, "quantity")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency key is required")
        account = self._account(user_id)
        if key in account["idempotency"]:
            return {"order": dict(account["idempotency"][key]), "account": self.account(user_id)}
        quote = self._latest_quotes.get(symbol)
        if order_type == "market":
            if self.state != "running":
                raise ValueError("SIM_NOT_RUNNING")
            if not quote:
                raise ValueError("current replay quote is unavailable")
            limit_price = None
        else:
            limit_price = _positive_float(limit_price, "limit_price")
        positions = account["positions"]
        position = positions.get(symbol, {"quantity": 0, "averagePrice": 0.0, "reservedQuantity": 0})
        if side == "sell" and int(position["quantity"]) - int(position.get("reservedQuantity") or 0) < quantity:
            raise ValueError("insufficient position quantity")
        reservation = 0.0
        if side == "buy":
            reservation = float(quote["ask"] if order_type == "market" else limit_price) * quantity
            if float(account["cash"]) - float(account["reservedCash"]) + 1e-9 < reservation:
                raise ValueError("insufficient simulation cash")
        order = {"order_id": str(uuid.uuid4()), "status": "accepted", "simulation": True, "runId": self.run_id,
            "symbol": symbol, "side": side, "qty": str(quantity), "order_type": order_type, "price": limit_price,
            "filled_price": None, "virtualSubmittedAt": self._format(self.cursor), "recordedAt": datetime.now(UTC).isoformat(),
            "reservedCash": reservation if side == "buy" else 0.0}
        account["orders"].append(order); account["idempotency"][key] = order
        if side == "buy":
            account["reservedCash"] = float(account["reservedCash"]) + reservation
        else:
            position = positions.setdefault(symbol, position)
            position["reservedQuantity"] = int(position.get("reservedQuantity") or 0) + quantity
        if order_type == "market":
            self._fill(account, order, quote["ask"] if side == "buy" else quote["bid"])
        elif self.state == "running":
            self._match(account, order, quote)
        return {"order": dict(order), "account": self.account(user_id)}

    def _match_orders(self, symbol: str) -> None:
        quote = self._latest_quotes.get(symbol)
        for account in self._accounts.values():
            for order in account["orders"]:
                if order["symbol"] == symbol and order["status"] == "accepted":
                    self._match(account, order, quote)

    def _match_all_orders(self) -> None:
        for symbol in tuple(self._latest_quotes):
            self._match_orders(symbol)

    def _match(self, account: dict[str, object], order: dict[str, object], quote: dict[str, float] | None) -> None:
        if not quote or order["status"] != "accepted":
            return
        if order["order_type"] == "market": price = quote["ask"] if order["side"] == "buy" else quote["bid"]
        elif order["side"] == "buy" and quote["ask"] <= float(order["price"]): price = quote["ask"]
        elif order["side"] == "sell" and quote["bid"] >= float(order["price"]): price = quote["bid"]
        else: return
        self._fill(account, order, price)

    def _fill(self, account: dict[str, object], order: dict[str, object], price: float) -> None:
        quantity, symbol = int(order["qty"]), str(order["symbol"])
        position = account["positions"].setdefault(symbol, {"quantity": 0, "averagePrice": 0.0, "reservedQuantity": 0})
        if order["side"] == "buy":
            account["reservedCash"] = max(0.0, float(account["reservedCash"]) - float(order.get("reservedCash") or 0))
            cost = price * quantity
            if float(account["cash"]) + 1e-9 < cost:
                order.update(status="rejected", reason="insufficient simulation cash at fill"); return
            old = int(position["quantity"]); old_cost = float(position["averagePrice"]) * old
            position["quantity"] = old + quantity; position["averagePrice"] = (old_cost + cost) / (old + quantity)
            account["cash"] = float(account["cash"]) - cost
        else:
            position["reservedQuantity"] = max(0, int(position.get("reservedQuantity") or 0) - quantity)
            if int(position["quantity"]) < quantity:
                order.update(status="rejected", reason="insufficient position quantity at fill"); return
            position["quantity"] = int(position["quantity"]) - quantity
            account["cash"] = float(account["cash"]) + price * quantity
        order.update(status="filled", filled_price=round(price, 6), virtualFilledAt=self._format(self.cursor))

    def _trigger_conditions(self, symbol: str, previous: float, price: float) -> None:
        for user_id, account in self._accounts.items():
            for condition in account["conditions"]:
                if condition["symbol"] != symbol or condition["status"] != "watching": continue
                target = float(condition["triggerPrice"])
                crossed = condition["direction"] == "atOrAbove" and previous < target <= price or condition["direction"] == "atOrBelow" and previous > target >= price
                if not crossed: continue
                condition.update(status="triggered", virtualTriggeredAt=self._format(self.cursor))
                if condition.get("executionEnabled") is False:
                    condition["status"] = "completed"; continue
                try:
                    result = self._submit_order(user_id=user_id, symbol=symbol, side=condition["side"], quantity=condition["quantity"],
                        order_type="limit", limit_price=condition["limitPrice"], idempotency_key=f"condition:{condition['id']}")
                    condition.update(status="completed", orderId=result["order"]["order_id"])
                except ValueError as exc:
                    condition.update(status="failed", error=str(exc))

    def _cancel_open(self) -> None:
        for account in self._accounts.values():
            for order in account["orders"]:
                if order["status"] != "accepted": continue
                order.update(status="canceled", virtualCanceledAt=self._format(self.cursor))
                if order["side"] == "buy": account["reservedCash"] = max(0.0, float(account["reservedCash"]) - float(order.get("reservedCash") or 0))
                else:
                    position = account["positions"].get(order["symbol"])
                    if position: position["reservedQuantity"] = max(0, int(position.get("reservedQuantity") or 0) - int(order["qty"]))
            for condition in account["conditions"]:
                if condition["status"] == "watching": condition["status"] = "canceled"

    def _account(self, user_id: str) -> dict[str, object]:
        key = str(user_id or "").strip()
        if not key: raise ValueError("user_id is required")
        return self._accounts.setdefault(key, {"cash": INITIAL_CASH, "reservedCash": 0.0, "positions": {},
            "orders": [], "conditions": [], "idempotency": {}, "nextConditionId": 1})

    def _condition(self, user_id: str, condition_id: int) -> dict[str, object] | None:
        return next((item for item in self._account(user_id)["conditions"] if int(item["id"]) == int(condition_id)), None)

    def _symbol(self, value: object) -> str:
        symbol = str(value or "").strip().upper()
        if symbol not in REPLAY_SYMBOLS: raise ValueError(f"symbol is not available in {DATASET_ID}")
        return symbol

    def _require_simulation(self) -> None:
        if self.mode != "simulation" or self.run_id is None: raise ValueError("simulation mode is not active")

    def _restore_state(self) -> None:
        if not self.state_store: return
        snapshot = self.state_store.load_active()
        if not isinstance(snapshot, dict) or snapshot.get("mode") != "simulation" or not snapshot.get("runId"): return
        self.mode, self.run_id = "simulation", str(snapshot["runId"])
        self.state = "paused" if snapshot.get("state") == "running" else str(snapshot.get("state") or "paused")
        self.requested_speed = int(snapshot.get("requestedSpeed") or self.default_speed)
        self.cursor = parse_timestamp(snapshot.get("virtualTime") or DATASET_START.isoformat())
        self.last_sequence = int(snapshot.get("processedEventCount") or 0)
        self._accounts = dict(snapshot.get("accounts") or {})
        self._latest_quotes = dict(snapshot.get("latestQuotes") or {})
        self._latest_trades = dict(snapshot.get("latestTrades") or {})
        self._normalize_restored_accounts()
        self._reset_anchor(); self._persist()

    def _normalize_restored_accounts(self) -> None:
        for account in self._accounts.values():
            orders = account.setdefault("orders", [])
            conditions = account.setdefault("conditions", [])
            by_id = {str(order.get("order_id")): order for order in orders if isinstance(order, dict)}
            restored_idempotency = account.get("idempotency")
            if isinstance(restored_idempotency, dict):
                account["idempotency"] = {
                    str(key): by_id[str(order.get("order_id"))]
                    for key, order in restored_idempotency.items()
                    if isinstance(order, dict) and str(order.get("order_id")) in by_id
                }
            else:
                account["idempotency"] = {}
            highest_condition_id = max(
                (int(condition.get("id") or 0) for condition in conditions if isinstance(condition, dict)),
                default=0,
            )
            account["nextConditionId"] = max(
                int(account.get("nextConditionId") or 1),
                highest_condition_id + 1,
            )

    def _persist(self) -> None:
        if self.state_store and self.run_id:
            self.state_store.save(self.run_id, {"datasetId": self.source.dataset_id, "mode": self.mode, "state": self.state,
                "virtualTime": self._format(self.cursor), "requestedSpeed": self.requested_speed,
                "processedEventCount": self.last_sequence, "latestQuotes": self._latest_quotes,
                "latestTrades": self._latest_trades, "accounts": self._accounts})

    def _reset_anchor(self, *, now: float | None = None) -> None:
        self._anchor_wall = self.clock() if now is None else now; self._anchor_virtual = self.cursor

    def _status(self) -> dict[str, object]:
        elapsed, duration = max(0.0, (self.cursor - DATASET_START).total_seconds()), (DATASET_END - DATASET_START).total_seconds()
        wall = 0.0 if self._run_started_wall is None else max(0.0, self.clock() - self._run_started_wall)
        target = min(DATASET_END, self._anchor_virtual + timedelta(seconds=max(0.0, self.clock() - self._anchor_wall) * self.requested_speed)) if self.state == "running" else self.cursor
        return {"datasetId": self.source.dataset_id, "mode": self.mode, "state": self.state, "runId": self.run_id,
            "virtualTime": self._format(self.cursor), "startTime": self._format(DATASET_START), "endTime": self._format(DATASET_END),
            "requestedSpeed": self.requested_speed, "effectiveSpeed": round(elapsed / wall if wall > 0 else 0.0, 3),
            "processedEventCount": self.last_sequence, "totalEventCount": self.source.total_events,
            "progress": round(min(1.0, elapsed / duration), 8), "lagMs": round(max(0.0, (target - self.cursor).total_seconds()) * 1000),
            "symbols": [{"symbol": symbol, "price": self._latest_trades.get(symbol)} for symbol in REPLAY_SYMBOLS]}

    @staticmethod
    def _format(value: datetime) -> str:
        return value.astimezone(KST).isoformat(timespec="seconds")


def _positive_float(value: object, field: str) -> float:
    try: parsed = float(value)
    except (TypeError, ValueError) as exc: raise ValueError(f"{field} must be a positive number") from exc
    if parsed <= 0: raise ValueError(f"{field} must be a positive number")
    return parsed


def _nonnegative_float(value: object, field: str) -> float:
    try: parsed = float(value)
    except (TypeError, ValueError) as exc: raise ValueError(f"{field} must be a non-negative number") from exc
    if parsed < 0: raise ValueError(f"{field} must be a non-negative number")
    return parsed


def _positive_int(value: object, field: str) -> int:
    try: parsed = int(value)
    except (TypeError, ValueError) as exc: raise ValueError(f"{field} must be a positive whole number") from exc
    if parsed <= 0 or float(value) != parsed: raise ValueError(f"{field} must be a positive whole number")
    return parsed


def _one_of(value: object, choices: set[str], field: str) -> str:
    parsed = str(value or "").strip()
    if parsed not in choices: raise ValueError(f"{field} must be one of {sorted(choices)}")
    return parsed
