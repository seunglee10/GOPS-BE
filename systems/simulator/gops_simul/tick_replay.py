"""Deterministic virtual clock and quote-driven trading ledger."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo

from gops_simul.dataset import (
    ALLOWED_SPEEDS,
    DATASET_END,
    DATASET_ID,
    DATASET_START,
    REPLAY_SYMBOL_SET,
    REPLAY_SYMBOLS,
    in_half_open_window,
    parse_timestamp,
)
from gops_simul.order_flow import ReplayOrderFlowProjection
from gops_simul.state_store import ReplayStateStore


KST = timezone(timedelta(hours=9))
MARKET_TIMEZONE = ZoneInfo("America/New_York")
LEGACY_REPLAY_SPEEDS = frozenset({20, 60, 300})
SYMBOL_STATUS_REFRESH_SECONDS = 0.25


def normalize_configured_replay_speed(value: object, *, fallback: int | None = None) -> int:
    try:
        speed = int(value)
    except (TypeError, ValueError):
        speed = -1
    if speed in ALLOWED_SPEEDS:
        return speed
    if speed in LEGACY_REPLAY_SPEEDS:
        return max(ALLOWED_SPEEDS)
    if fallback in ALLOWED_SPEEDS:
        return int(fallback)
    raise ValueError(f"speed must be one of {ALLOWED_SPEEDS}")


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
        if str(self.payload.get("S") or "").upper() not in REPLAY_SYMBOL_SET:
            raise ValueError("unsupported replay symbol")


@dataclass(frozen=True)
class ReplayReadSnapshot:
    mode: str
    state: str
    run_id: str | None
    cursor: datetime
    last_sequence: int
    daily_candles: Mapping[str, Mapping[str, Mapping[str, object]]]


class ReplayRunChangedError(RuntimeError):
    """A projection completed after its captured replay run was replaced."""


class ReplayEventSource(Protocol):
    dataset_id: str
    total_events: int
    def previous_close_snapshot(self) -> dict[str, float]: ...
    def events_after_sequence(self, sequence: int, limit: int) -> list[ReplayEvent]: ...
    def events_after(self, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]: ...
    def events_between(self, after_sequence: int, through_sequence: int, limit: int) -> list[ReplayEvent]: ...
    def events_for_symbol_after(self, symbol: str, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]: ...
    def events_for_symbol_window(
        self,
        symbol: str,
        sequence: int,
        start: datetime,
        through: datetime,
        limit: int,
    ) -> list[ReplayEvent]: ...
    def candle_snapshot(self, symbol: str, interval: str, through: datetime, limit: int) -> dict[str, object]: ...


class InMemoryReplayEventSource:
    dataset_id = DATASET_ID

    def __init__(
        self,
        events: Iterable[ReplayEvent],
        *,
        previous_closes: dict[str, float] | None = None,
    ) -> None:
        self._events = sorted(events, key=lambda event: event.sequence)
        sequences = [event.sequence for event in self._events]
        if len(sequences) != len(set(sequences)):
            raise ValueError("replay event sequences must be unique")
        self.total_events = len(self._events)
        self._previous_closes = {
            symbol: float(value)
            for raw_symbol, value in (previous_closes or {}).items()
            if (symbol := str(raw_symbol).strip().upper()) in REPLAY_SYMBOL_SET
            and float(value) > 0
        }

    def previous_close_snapshot(self) -> dict[str, float]:
        return dict(self._previous_closes)

    def events_after_sequence(self, sequence: int, limit: int) -> list[ReplayEvent]:
        return [event for event in self._events if event.sequence > sequence][:limit]

    def events_after(self, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]:
        return [
            event
            for event in self.events_after_sequence(sequence, limit)
            if event.timestamp <= through
        ]

    def events_between(self, after_sequence: int, through_sequence: int, limit: int) -> list[ReplayEvent]:
        return [
            event
            for event in self._events
            if after_sequence < event.sequence <= through_sequence
        ][:limit]

    def events_for_symbol_after(self, symbol: str, sequence: int, through: datetime, limit: int) -> list[ReplayEvent]:
        normalized = symbol.strip().upper()
        return [
            event for event in self._events
            if event.sequence > sequence
            and event.timestamp <= through
            and str(event.payload.get("S") or "").upper() == normalized
            and event.payload.get("T") in {"q", "t"}
        ][:limit]

    def events_for_symbol_window(
        self,
        symbol: str,
        sequence: int,
        start: datetime,
        through: datetime,
        limit: int,
    ) -> list[ReplayEvent]:
        normalized = symbol.strip().upper()
        return [
            event for event in self._events
            if event.sequence > sequence
            and start <= event.timestamp <= through
            and str(event.payload.get("S") or "").upper() == normalized
            and event.payload.get("T") in {"q", "t"}
        ][:limit]

    def candle_snapshot(self, symbol: str, interval: str, through: datetime, limit: int) -> dict[str, object]:
        from gops_simul.clickhouse import INTERVAL_SECONDS, replay_candle_payload
        normalized = symbol.strip().upper()
        if normalized not in REPLAY_SYMBOL_SET:
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
        default_speed = normalize_configured_replay_speed(default_speed)
        self.source = source
        self.clock = clock
        self.default_speed = default_speed
        self.max_events_per_pump = max(1, max_events_per_pump)
        self.state_store = state_store
        self._lock = threading.RLock()
        self._order_flow_lock = threading.RLock()
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
        self._event_buffer: deque[ReplayEvent] = deque()
        self._latest_quotes: dict[str, dict[str, object]] = {}
        self._quote_snapshot: dict[str, dict[str, object]] = {}
        self._latest_trades: dict[str, float] = {}
        self._previous_closes = source.previous_close_snapshot()
        self._opening_trades: dict[str, float] = {}
        self._daily_candles: dict[str, dict[str, dict[str, object]]] = {}
        self._daily_snapshot_dirty = True
        self._published_daily_candles: Mapping[str, Mapping[str, Mapping[str, object]]] = MappingProxyType({})
        self._symbol_status_snapshot: list[dict[str, object]] = []
        self._symbol_status_dirty = True
        self._symbol_status_refreshed_at = float("-inf")
        self._order_flow = ReplayOrderFlowProjection(source)
        self._restore_state()
        with self._order_flow_lock:
            self._order_flow.reset(self.run_id)
        self._read_snapshot = self._build_read_snapshot()
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
                self._publish_read_snapshot()
                with self._order_flow_lock:
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

    def pump(self) -> None:
        """Advance replay state without copying the 502-symbol HTTP status payload."""
        with self._lock:
            self._pump()
            self._publish_read_snapshot()
            self._status_snapshot = self._status()

    def status_snapshot(self) -> dict[str, object]:
        snapshot = self._status_snapshot
        return {
            **snapshot,
            "symbols": [dict(item) for item in snapshot.get("symbols", [])],
        }

    def latest_quote(self, symbol: str) -> dict[str, object] | None:
        quote = self._quote_snapshot.get(symbol.strip().upper())
        return {"bid": quote["bid"], "ask": quote["ask"]} if quote else None

    def latest_quote_details(self, symbol: str) -> dict[str, object] | None:
        quote = self._quote_snapshot.get(symbol.strip().upper())
        return dict(quote) if quote else None

    def latest_quotes_details(self, symbols: Iterable[str]) -> dict[str, dict[str, object]]:
        snapshot = self._quote_snapshot
        return {
            symbol: dict(snapshot[symbol])
            for symbol in dict.fromkeys(str(value).strip().upper() for value in symbols)
            if symbol in snapshot
        }

    def execution_events(self, *, after_sequence: int, limit: int = 50_000) -> dict[str, object]:
        normalized_after = max(0, int(after_sequence))
        snapshot = self._read_snapshot
        self._require_read_snapshot_simulation(snapshot)
        run_id = snapshot.run_id
        virtual_time = snapshot.cursor
        through_sequence = snapshot.last_sequence
        events = self.source.events_between(normalized_after, through_sequence, max(1, int(limit)))
        self._ensure_read_snapshot_run(snapshot)
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
        self._ensure_read_snapshot_run(snapshot)
        next_sequence = events[-1].sequence if events else normalized_after
        return {
            "runId": run_id,
            "virtualTime": self._format(virtual_time),
            "afterSequence": normalized_after,
            "nextSequence": next_sequence,
            "caughtUp": next_sequence >= through_sequence,
            "quotes": quotes,
        }

    def emitted_events(self) -> list[ReplayEvent]:
        with self._lock:
            return list(self._emitted)

    def candle_snapshot(self, symbol: str, interval: str, limit: int = 2000) -> dict[str, object]:
        snapshot = self._read_snapshot
        self._require_read_snapshot_simulation(snapshot)
        if interval in {"1D", "1d"}:
            payload = self._daily_candle_snapshot(symbol, interval, limit, snapshot)
        else:
            payload = self.source.candle_snapshot(symbol, interval, snapshot.cursor, limit)
        self._ensure_read_snapshot_run(snapshot)
        return payload

    def order_flow_snapshot(
        self,
        symbol: str,
        *,
        after_sequence: int | None = None,
        latest_only: bool = False,
        window_minutes: int | None = None,
    ) -> dict[str, object]:
        snapshot = self._read_snapshot
        self._require_read_snapshot_simulation(snapshot)
        with self._order_flow_lock:
            payload = self._order_flow.snapshot(
                symbol,
                through=snapshot.cursor,
                run_id=str(snapshot.run_id),
                after_sequence=after_sequence,
                latest_only=latest_only,
                window_minutes=window_minutes,
            )
        self._ensure_read_snapshot_run(snapshot)
        return payload

    def _new_run(self) -> None:
        if self.run_id and self.state_store:
            self.state_store.delete(self.run_id)
        self.mode, self.state, self.run_id = "simulation", "ready", str(uuid.uuid4())
        self.requested_speed, self.cursor, self.last_sequence = self.default_speed, DATASET_START, 0
        self._emitted.clear(); self._event_buffer.clear(); self._latest_quotes.clear(); self._latest_trades.clear(); self._opening_trades.clear(); self._daily_candles.clear()
        self._daily_snapshot_dirty = True
        self._invalidate_symbol_status(force=True)
        self._publish_quote_snapshot()
        self._publish_read_snapshot()
        with self._order_flow_lock:
            self._order_flow.reset(self.run_id)
        self._run_started_wall = None
        self._reset_anchor(); self._persist()

    def _pump(self) -> None:
        if self.mode != "simulation" or self.state != "running":
            return
        now = self.clock()
        target = min(DATASET_END, self._anchor_virtual + timedelta(seconds=max(0.0, now - self._anchor_wall) * self.requested_speed))
        if not self._event_buffer:
            self._event_buffer.extend(
                self.source.events_after_sequence(self.last_sequence, self.max_events_per_pump)
            )
        events: list[ReplayEvent] = []
        while self._event_buffer and len(events) < self.max_events_per_pump:
            event = self._event_buffer[0]
            if event.timestamp > target:
                break
            events.append(self._event_buffer.popleft())
        for event in events:
            self._apply_event(event)
        if events:
            self.last_sequence = events[-1].sequence
            self._publish_quote_snapshot()
        if len(events) >= self.max_events_per_pump and not self._event_buffer:
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
            self._opening_trades.setdefault(symbol, price)
            self._latest_trades[symbol] = price
            self._invalidate_symbol_status()
            self._update_daily_candle(symbol, event, price)

    def _update_daily_candle(self, symbol: str, event: ReplayEvent, price: float) -> None:
        self._daily_snapshot_dirty = True
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

    def _daily_candle_snapshot(
        self,
        symbol: str,
        interval: str,
        limit: int,
        snapshot: ReplayReadSnapshot,
    ) -> dict[str, object]:
        from gops_simul.clickhouse import replay_candle_payload

        normalized = self._symbol(symbol)
        candles = []
        for market_date, candle in sorted(snapshot.daily_candles.get(normalized, {}).items())[-max(1, int(limit)):]:
            bucket_end = market_midnight_utc(
                (datetime.fromisoformat(market_date).date() + timedelta(days=1)).isoformat()
            )
            candles.append({
                **candle,
                "interval": interval,
                "isClosed": bucket_end <= snapshot.cursor,
            })
        return replay_candle_payload(normalized, interval, candles, snapshot.cursor)

    def _symbol(self, value: object) -> str:
        symbol = str(value or "").strip().upper()
        if symbol not in REPLAY_SYMBOL_SET:
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
        self.requested_speed = normalize_configured_replay_speed(
            snapshot.get("requestedSpeed"),
            fallback=self.default_speed,
        )
        self.cursor = parse_timestamp(snapshot.get("virtualTime") or DATASET_START.isoformat())
        self.last_sequence = int(snapshot.get("processedEventCount") or 0)
        self._latest_quotes = dict(snapshot.get("latestQuotes") or {})
        self._publish_quote_snapshot()
        self._latest_trades = dict(snapshot.get("latestTrades") or {})
        self._daily_candles = normalize_restored_daily_candles(snapshot.get("dailyCandles"))
        self._daily_snapshot_dirty = True
        self._opening_trades = normalize_restored_opening_trades(
            snapshot.get("openingTrades"),
            self._daily_candles,
        )
        self._invalidate_symbol_status(force=True)
        self._reset_anchor(); self._persist()

    def _persist(self) -> None:
        if self.state_store and self.run_id:
            self.state_store.save(self.run_id, {"datasetId": self.source.dataset_id, "mode": self.mode, "state": self.state,
                "virtualTime": self._format(self.cursor), "requestedSpeed": self.requested_speed,
                "processedEventCount": self.last_sequence, "latestQuotes": self._latest_quotes,
                "latestTrades": self._latest_trades, "openingTrades": self._opening_trades,
                "dailyCandles": self._daily_candles})

    def _reset_anchor(self, *, now: float | None = None) -> None:
        self._anchor_wall = self.clock() if now is None else now; self._anchor_virtual = self.cursor

    def _publish_quote_snapshot(self) -> None:
        self._quote_snapshot = {
            symbol: dict(quote)
            for symbol, quote in self._latest_quotes.items()
        }

    def _build_read_snapshot(self) -> ReplayReadSnapshot:
        if self._daily_snapshot_dirty:
            self._published_daily_candles = MappingProxyType({
                symbol: MappingProxyType({
                    market_date: MappingProxyType(dict(candle))
                    for market_date, candle in candles.items()
                })
                for symbol, candles in self._daily_candles.items()
            })
            self._daily_snapshot_dirty = False
        return ReplayReadSnapshot(
            mode=self.mode,
            state=self.state,
            run_id=self.run_id,
            cursor=self.cursor,
            last_sequence=self.last_sequence,
            daily_candles=self._published_daily_candles,
        )

    def _publish_read_snapshot(self) -> None:
        self._read_snapshot = self._build_read_snapshot()

    @staticmethod
    def _require_read_snapshot_simulation(snapshot: ReplayReadSnapshot) -> None:
        if snapshot.mode != "simulation" or snapshot.run_id is None:
            raise ValueError("simulation mode is not active")

    def _ensure_read_snapshot_run(self, snapshot: ReplayReadSnapshot) -> None:
        current = self._read_snapshot
        if current.mode != "simulation" or current.run_id != snapshot.run_id:
            raise ReplayRunChangedError("simulation run changed during replay projection")

    def _invalidate_symbol_status(self, *, force: bool = False) -> None:
        self._symbol_status_dirty = True
        if force:
            self._symbol_status_snapshot = []
            self._symbol_status_refreshed_at = float("-inf")

    def _current_symbol_statuses(self) -> list[dict[str, object]]:
        now = self.clock()
        should_refresh = not self._symbol_status_snapshot or (
            self._symbol_status_dirty
            and now - self._symbol_status_refreshed_at >= SYMBOL_STATUS_REFRESH_SECONDS
        )
        if should_refresh:
            self._symbol_status_snapshot = [
                self._symbol_status(symbol)
                for symbol in REPLAY_SYMBOLS
            ]
            self._symbol_status_dirty = False
            self._symbol_status_refreshed_at = now
        return self._symbol_status_snapshot

    def _status(self) -> dict[str, object]:
        elapsed, duration = max(0.0, (self.cursor - DATASET_START).total_seconds()), (DATASET_END - DATASET_START).total_seconds()
        wall = 0.0 if self._run_started_wall is None else max(0.0, self.clock() - self._run_started_wall)
        target = min(DATASET_END, self._anchor_virtual + timedelta(seconds=max(0.0, self.clock() - self._anchor_wall) * self.requested_speed)) if self.state == "running" else self.cursor
        return {"datasetId": self.source.dataset_id, "mode": self.mode, "state": self.state, "runId": self.run_id,
            "virtualTime": self._format(self.cursor), "startTime": self._format(DATASET_START), "endTime": self._format(DATASET_END),
            "requestedSpeed": self.requested_speed, "effectiveSpeed": round(elapsed / wall if wall > 0 else 0.0, 3),
            "processedEventCount": self.last_sequence, "totalEventCount": self.source.total_events,
            "progress": round(min(1.0, elapsed / duration), 8), "lagMs": round(max(0.0, (target - self.cursor).total_seconds()) * 1000),
            "symbols": self._current_symbol_statuses()}

    def _symbol_status(self, symbol: str) -> dict[str, object]:
        price = self._latest_trades.get(symbol)
        previous_close = self._previous_closes.get(symbol)
        return {
            "symbol": symbol,
            "price": price,
            "previousClose": previous_close,
            "changePercent": replay_change_percent(previous_close, price),
        }

    def _capture_status(self) -> dict[str, object]:
        self._publish_read_snapshot()
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
        if normalized_symbol not in REPLAY_SYMBOL_SET or not isinstance(rows, dict):
            continue
        restored[normalized_symbol] = {
            str(market_date): dict(candle)
            for market_date, candle in rows.items()
            if isinstance(candle, dict)
        }
    return restored


def normalize_restored_opening_trades(
    value: object,
    daily_candles: dict[str, dict[str, dict[str, object]]],
) -> dict[str, float]:
    source = value if isinstance(value, dict) else {}
    restored: dict[str, float] = {}
    for symbol in REPLAY_SYMBOLS:
        opening_price = source.get(symbol)
        if opening_price is None:
            symbol_candles = daily_candles.get(symbol, {})
            first_candle = next(iter(sorted(symbol_candles.items())), (None, None))[1]
            opening_price = first_candle.get("open") if isinstance(first_candle, dict) else None
        try:
            numeric = float(opening_price)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            restored[symbol] = numeric
    return restored


def replay_change_percent(previous_close: float | None, current_price: float | None) -> float | None:
    if previous_close is None or current_price is None or previous_close <= 0:
        return None
    return round((current_price / previous_close - 1.0) * 100.0, 6)


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
