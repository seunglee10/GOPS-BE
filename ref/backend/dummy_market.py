from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.schemas import Candle, MarketSnapshotResponse


DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"]
SUPPORTED_TIMEFRAMES = {"1s", "5s", "15s", "1m", "5m", "15m", "1h", "1d"}
TIMEFRAME_SECONDS = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}
BASE_PRICES = {
    "AAPL": 214.0,
    "MSFT": 493.0,
    "NVDA": 152.0,
    "TSLA": 327.0,
    "SPY": 642.0,
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def floor_to_timeframe(dt: datetime, timeframe: str) -> datetime:
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch_seconds = int(dt.timestamp())
    floored = epoch_seconds - (epoch_seconds % seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def clamp_snapshot_limit(limit: int) -> int:
    return min(1000, max(50, limit))


class DummyMarketData:
    def __init__(self) -> None:
        self._candles: dict[tuple[str, str], list[Candle]] = {}
        self._rng = random.Random(26426)

    def validate_symbols(self, symbols: list[str]) -> list[str]:
        clean = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not clean:
            return ["AAPL"]
        unsupported = [symbol for symbol in clean if symbol not in DEFAULT_SYMBOLS]
        if unsupported:
            raise ValueError(f"Unsupported symbol: {', '.join(unsupported)}")
        return clean

    def validate_timeframe(self, timeframe: str) -> str:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        return timeframe

    def snapshot(self, symbols: list[str], timeframe: str, limit: int) -> MarketSnapshotResponse:
        symbols = self.validate_symbols(symbols)
        timeframe = self.validate_timeframe(timeframe)
        clamped_limit = clamp_snapshot_limit(limit)
        candles_by_symbol: dict[str, list[Candle]] = {}
        for symbol in symbols:
            candles = self._ensure_history(symbol, timeframe, clamped_limit)
            candles_by_symbol[symbol] = candles[-limit:]
        return MarketSnapshotResponse(
            provider="dummy",
            timeframe=timeframe,  # type: ignore[arg-type]
            generatedAt=utc_now_iso(),
            symbols=symbols,
            candlesBySymbol=candles_by_symbol,
        )

    def next_event_batch(self, symbols: list[str], timeframe: str) -> list[dict[str, Any]]:
        symbols = self.validate_symbols(symbols)
        timeframe = self.validate_timeframe(timeframe)
        current_bucket = floor_to_timeframe(datetime.now(UTC), timeframe)
        events: list[dict[str, Any]] = []
        for symbol in symbols:
            candles = self._ensure_history(symbol, timeframe, 300)
            last_dt = parse_iso(candles[-1].timestamp)
            if last_dt < current_bucket:
                next_timestamp = min(last_dt + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]), current_bucket)
                events.append(self._append_new_bar(symbol, timeframe, candles, next_timestamp))
            else:
                events.append(self._update_last_bar(symbol, timeframe, candles))
        return events

    def _ensure_history(self, symbol: str, timeframe: str, min_count: int) -> list[Candle]:
        key = (symbol, timeframe)
        if key not in self._candles:
            self._candles[key] = self._build_history(symbol, timeframe, max(min_count, 300))
        elif len(self._candles[key]) < min_count:
            missing = min_count - len(self._candles[key])
            first = parse_iso(self._candles[key][0].timestamp)
            older_end = first - timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
            older = self._build_history(symbol, timeframe, missing, end_at=older_end)
            self._candles[key] = older + self._candles[key]
        current_bucket = floor_to_timeframe(datetime.now(UTC), timeframe)
        if parse_iso(self._candles[key][-1].timestamp) > current_bucket:
            self._candles[key] = self._build_history(symbol, timeframe, max(min_count, len(self._candles[key])), end_at=current_bucket)
        return self._candles[key]

    def _build_history(self, symbol: str, timeframe: str, count: int, end_at: datetime | None = None) -> list[Candle]:
        seconds = TIMEFRAME_SECONDS[timeframe]
        end = floor_to_timeframe(end_at or datetime.now(UTC), timeframe)
        start = end - timedelta(seconds=seconds * (count - 1))
        base = BASE_PRICES[symbol]
        candles: list[Candle] = []
        price = base
        for index in range(count):
            timestamp = start + timedelta(seconds=seconds * index)
            wave = math.sin(index / 9.0) * 0.004 + math.sin(index / 23.0) * 0.007
            drift = (index - count / 2) * 0.00003
            open_price = price
            close_price = max(1.0, open_price * (1 + wave + drift))
            high = max(open_price, close_price) * (1 + 0.0015 + abs(math.sin(index)) * 0.002)
            low = min(open_price, close_price) * (1 - 0.0015 - abs(math.cos(index)) * 0.002)
            volume = int(80000 + abs(math.sin(index / 5.0)) * 90000 + (index % 17) * 2300)
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,  # type: ignore[arg-type]
                    timestamp=timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    open=round(open_price, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close_price, 4),
                    volume=volume,
                    vwap=round((open_price + high + low + close_price) / 4, 4),
                    tradeCount=max(1, int(volume / 110)),
                    finalized=index < count - 1,
                )
            )
            price = close_price
        return candles

    def _append_new_bar(self, symbol: str, timeframe: str, candles: list[Candle], timestamp: datetime) -> dict[str, Any]:
        last = candles[-1]
        candles[-1] = last.model_copy(update={"finalized": True})
        movement = self._rng.uniform(-0.004, 0.004)
        close = max(1.0, last.close * (1 + movement))
        high = max(last.close, close) * (1 + self._rng.uniform(0.0005, 0.003))
        low = min(last.close, close) * (1 - self._rng.uniform(0.0005, 0.003))
        volume = int(last.volume * self._rng.uniform(0.75, 1.35))
        candle = Candle(
            symbol=symbol,
            timeframe=timeframe,  # type: ignore[arg-type]
            timestamp=timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            open=round(last.close, 4),
            high=round(high, 4),
            low=round(low, 4),
            close=round(close, 4),
            volume=volume,
            vwap=round((last.close + high + low + close) / 4, 4),
            tradeCount=max(1, int(volume / 100)),
            finalized=False,
        )
        candles.append(candle)
        if len(candles) > 1600:
            del candles[: len(candles) - 1600]
        return self._bar_event("bar", candle)

    def _update_last_bar(self, symbol: str, timeframe: str, candles: list[Candle]) -> dict[str, Any]:
        last = candles[-1]
        movement = self._rng.uniform(-0.0025, 0.0025)
        close = max(1.0, last.close * (1 + movement))
        high = max(last.high, close)
        low = min(last.low, close)
        volume = last.volume + int(self._rng.uniform(1200, 8000))
        candle = last.model_copy(
            update={
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "volume": volume,
                "vwap": round((last.open + high + low + close) / 4, 4),
                "tradeCount": (last.tradeCount or 0) + max(1, int(volume / 7000)),
                "finalized": False,
            }
        )
        candles[-1] = candle
        return self._bar_event("updatedBar", candle)

    def _bar_event(self, event_type: str, candle: Candle) -> dict[str, Any]:
        received_at = utc_now_iso()
        return {
            "type": event_type,
            "provider": "dummy",
            "symbol": candle.symbol,
            "timeframe": candle.timeframe,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "vwap": candle.vwap,
            "tradeCount": candle.tradeCount,
            "timestamp": candle.timestamp,
            "receivedAt": received_at,
        }
