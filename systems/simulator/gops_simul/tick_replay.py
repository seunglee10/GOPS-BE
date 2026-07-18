"""Deterministic virtual clock and quote-driven trading ledger."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time, timedelta, timezone
from typing import Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

from gops_simul.dataset import ALLOWED_SPEEDS, DATASET_END, DATASET_ID, DATASET_START, REPLAY_SYMBOLS, in_half_open_window, parse_timestamp
from gops_simul.order_flow import ReplayOrderFlowProjection
from gops_simul.state_store import ReplayStateStore


KST = timezone(timedelta(hours=9))
MARKET_TIMEZONE = ZoneInfo("America/New_York")


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
    def events_for_symbol_after(self, symbol: str, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]: ...
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

    def events_for_symbol_after(self, symbol: str, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]:
        normalized = symbol.strip().upper()
        return [
            event for event in self._events
            if event.sequence > sequence
            and event.timestamp <= through
            and str(event.payload.get("S") or "").upper() == normalized
            and event.payload.get("T") in {"q", "t"}
        ][:limit]

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
        self._latest_quotes: dict[str, dict[str, object]] = {}
        self._latest_trades: dict[str, float] = {}
        self._daily_candles: dict[str, dict[str, dict[str, object]]] = {}
        self._order_flow = ReplayOrderFlowProjection(source)
        self._restore_state()
        self._status_snapshot = self._status()

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
                self._order_flow.reset()
                if old_run and self.state_store:
                    self.state_store.delete(old_run)
            return self._capture_status()

    def start(self) -> dict[str, object]:
        if self.source.total_events <= 0:
            raise ValueError(f"replay dataset is not READY: {self.source.dataset_id}")
        with self._lock:
            self._new_run()
            self._reset_anchor()
            self.state = "running"
            self._run_started_wall = self.clock()
            self._persist()
            return self._capture_status()

    def resume(self) -> dict[str, object]:
        with self._lock:
            self._require_simulation()
            if self.state != "completed":
                self._reset_anchor()
                self.state = "running"
                if self._run_started_wall is None:
                    self._run_started_wall = self.clock()
                self._persist()
            return self._capture_status()

    def pause(self) -> dict[str, object]:
        with self._lock:
            self._require_simulation()
            self._pump()
            if self.state != "completed":
                self.state = "paused"
            self._reset_anchor()
            self._persist()
            return self._capture_status()

    def restart(self) -> dict[str, object]:
        with self._lock:
            self._require_simulation()
            self._new_run()
            return self._capture_status()

    def set_speed(self, speed: int) -> dict[str, object]:
        if speed not in ALLOWED_SPEEDS:
            raise ValueError(f"speed must be one of {ALLOWED_SPEEDS}")
        with self._lock:
            self._pump()
            self.requested_speed = speed
            self._reset_anchor()
            self._persist()
            return self._capture_status()

    def status(self) -> dict[str, object]:
        with self._lock:
            self._pump()
            return self._capture_status()

    def status_snapshot(self) -> dict[str, object]:
        snapshot = self._status_snapshot
        return {
            **snapshot,
            "symbols": [dict(item) for item in snapshot.get("symbols", [])],
        }

    def latest_quote(self, symbol: str) -> dict[str, object] | None:
        with self._lock:
            quote = self._latest_quotes.get(symbol.strip().upper())
            return {"bid": quote["bid"], "ask": quote["ask"]} if quote else None

    def latest_quote_details(self, symbol: str) -> dict[str, object] | None:
        with self._lock:
            quote = self._latest_quotes.get(symbol.strip().upper())
            return dict(quote) if quote else None

    def execution_events(self, *, after_sequence: int, limit: int = 50_000) -> dict[str, object]:
        with self._lock:
            self._pump()
            self._require_simulation()
            events = self.source.events_after(max(0, int(after_sequence)), self.cursor, max(1, int(limit)))
            quotes = []
            for event in events:
                payload = event.payload
                if payload.get("T") != "q":
                    continue
                bid = _nonnegative_float(payload.get("bp"), "bid")
                ask = _nonnegative_float(payload.get("ap"), "ask")
                quotes.append({
                    "sequence": event.sequence,
                    "symbol": str(payload.get("S") or "").upper(),
                    "bid": bid,
                    "ask": ask,
                    "timestamp": self._format(event.timestamp),
                })
            next_sequence = events[-1].sequence if events else max(0, int(after_sequence))
            return {
                "runId": self.run_id,
                "virtualTime": self._format(self.cursor),
                "afterSequence": max(0, int(after_sequence)),
                "nextSequence": next_sequence,
                "caughtUp": next_sequence >= self.last_sequence,
                "quotes": quotes,
            }

    def emitted_events(self) -> list[ReplayEvent]:
        with self._lock:
            return list(self._emitted)

    def candle_snapshot(self, symbol: str, interval: str, limit: int = 2000) -> dict[str, object]:
        with self._lock:
            self._pump()
            self._capture_status()
            if interval in {"1D", "1d"}:
                return self._daily_candle_snapshot(symbol, interval, limit)
            return self.source.candle_snapshot(symbol, interval, self.cursor, limit)

    def order_flow_snapshot(
        self,
        symbol: str,
        *,
        after_sequence: int | None = None,
        latest_only: bool = False,
    ) -> dict[str, object]:
        with self._lock:
            self._pump()
            self._require_simulation()
            return self._order_flow.snapshot(
                symbol,
                through=self.cursor,
                run_id=str(self.run_id),
                after_sequence=after_sequence,
                latest_only=latest_only,
            )

    def _new_run(self) -> None:
        if self.run_id and self.state_store:
            self.state_store.delete(self.run_id)
        self.mode, self.state, self.run_id = "simulation", "ready", str(uuid.uuid4())
        self.requested_speed, self.cursor, self.last_sequence = self.default_speed, DATASET_START, 0
        self._emitted.clear(); self._latest_quotes.clear(); self._latest_trades.clear(); self._daily_candles.clear()
        self._order_flow.reset(self.run_id)
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
            self._latest_quotes[symbol] = {
                "bid": bid,
                "ask": ask,
                "sequence": event.sequence,
                "virtualTime": self._format(event.timestamp),
            }
        elif payload.get("T") == "t":
            price = _positive_float(payload.get("p"), "price")
            self._latest_trades[symbol] = price
            self._update_daily_candle(symbol, event, price)

    def _update_daily_candle(self, symbol: str, event: ReplayEvent, price: float) -> None:
        market_date = event.timestamp.astimezone(MARKET_TIMEZONE).date().isoformat()
        symbol_candles = self._daily_candles.setdefault(symbol, {})
        current = symbol_candles.get(market_date)
        size = _nonnegative_float(event.payload.get("s") or 0, "size")
        if current is None:
            timestamp = market_midnight_utc(market_date).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            symbol_candles[market_date] = {
                "symbol": symbol,
                "interval": "1D",
                "timestamp": timestamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": size,
                "tradeCount": 1,
                "source": "simulation_replay",
                "feed": "mixed",
                "sourceInterval": "trades",
            }
            return
        current.update({
            "high": max(float(current["high"]), price),
            "low": min(float(current["low"]), price),
            "close": price,
            "volume": float(current.get("volume") or 0) + size,
            "tradeCount": int(current.get("tradeCount") or 0) + 1,
        })

    def _daily_candle_snapshot(self, symbol: str, interval: str, limit: int) -> dict[str, object]:
        from gops_simul.clickhouse import replay_candle_payload

        normalized = self._symbol(symbol)
        candles = []
        for market_date, candle in sorted(self._daily_candles.get(normalized, {}).items())[-max(1, int(limit)):]:
            bucket_end = market_midnight_utc(
                (datetime.fromisoformat(market_date).date() + timedelta(days=1)).isoformat()
            )
            candles.append({
                **candle,
                "interval": interval,
                "isClosed": bucket_end <= self.cursor,
            })
        return replay_candle_payload(normalized, interval, candles, self.cursor)

    def _symbol(self, value: object) -> str:
        symbol = str(value or "").strip().upper()
        if symbol not in REPLAY_SYMBOLS:
            raise ValueError(f"symbol is not available in {DATASET_ID}")
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
        self._latest_quotes = dict(snapshot.get("latestQuotes") or {})
        self._latest_trades = dict(snapshot.get("latestTrades") or {})
        self._daily_candles = normalize_restored_daily_candles(snapshot.get("dailyCandles"))
        self._reset_anchor(); self._persist()

    def _persist(self) -> None:
        if self.state_store and self.run_id:
            self.state_store.save(self.run_id, {"datasetId": self.source.dataset_id, "mode": self.mode, "state": self.state,
                "virtualTime": self._format(self.cursor), "requestedSpeed": self.requested_speed,
                "processedEventCount": self.last_sequence, "latestQuotes": self._latest_quotes,
                "latestTrades": self._latest_trades, "dailyCandles": self._daily_candles})

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

    def _capture_status(self) -> dict[str, object]:
        self._status_snapshot = self._status()
        return self.status_snapshot()

    @staticmethod
    def _format(value: datetime) -> str:
        return value.astimezone(KST).isoformat(timespec="seconds")


def market_midnight_utc(market_date: str) -> datetime:
    parsed = datetime.fromisoformat(market_date).date()
    return datetime.combine(parsed, datetime_time.min, tzinfo=MARKET_TIMEZONE).astimezone(UTC)


def normalize_restored_daily_candles(value: object) -> dict[str, dict[str, dict[str, object]]]:
    if not isinstance(value, dict):
        return {}
    restored: dict[str, dict[str, dict[str, object]]] = {}
    for symbol, rows in value.items():
        normalized_symbol = str(symbol).strip().upper()
        if normalized_symbol not in REPLAY_SYMBOLS or not isinstance(rows, dict):
            continue
        restored[normalized_symbol] = {
            str(market_date): dict(candle)
            for market_date, candle in rows.items()
            if isinstance(candle, dict)
        }
    return restored


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
