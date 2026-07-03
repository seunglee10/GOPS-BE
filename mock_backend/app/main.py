from __future__ import annotations

import asyncio
import math
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

Interval = Literal["1m", "5m", "10m", "1D", "1W", "1M"]
Ohlcv = tuple[float, float, float, float, int]

MARKET_TIMEZONE = ZoneInfo("America/New_York")
REGULAR_SESSION_START = time(9, 30)
REGULAR_SESSION_MINUTES = 390
MAX_RANGE_ITERATIONS = 40_000
TREND_ANCHOR_DAY = date(2024, 1, 2).toordinal()
MOCK_STREAM_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0)

INTERVAL_SECONDS: dict[Interval, int] = {
    "1m": 60,
    "5m": 300,
    "10m": 600,
    "1D": 86_400,
    "1W": 604_800,
    "1M": 2_592_000,
}


@dataclass(frozen=True)
class FakeSymbolProfile:
    symbol: str
    name: str
    sector: str
    base_price: float
    trend_bias: float
    volatility: float
    volume_base: int


@dataclass(frozen=True)
class SessionShape:
    day_number: int
    day_noise: float
    direction: float
    gap: float
    long_trend: float
    morning_phase: int
    random_walk_phase_a: int
    random_walk_phase_b: int
    event_roll: float
    event_index: int
    event_side: int


FAKE_SYMBOLS: tuple[FakeSymbolProfile, ...] = (
    FakeSymbolProfile("GOPS-ALP", "Alpinary Systems", "Synthetic Cloud Infrastructure", 118.0, 0.038, 1.12, 3_800),
    FakeSymbolProfile("GOPS-ION", "Ionbridge Dynamics", "Synthetic Energy Platforms", 72.0, 0.008, 0.98, 5_600),
    FakeSymbolProfile("GOPS-NOVA", "Novastra Fabrication", "Synthetic Robotics", 238.0, -0.052, 1.64, 2_900),
)
FAKE_SYMBOL_BY_SYMBOL = {profile.symbol: profile for profile in FAKE_SYMBOLS}

app = FastAPI(title="GOPS CDC Mock Chart Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://127.0.0.1:5176",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://localhost:5176",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/charts/symbols")
async def get_symbols() -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": profile.symbol,
                "name": profile.name,
                "sector": profile.sector,
                "isMock": True,
            }
            for profile in FAKE_SYMBOLS
        ]
    }


@app.get("/api/charts/candles")
async def get_candles(
    symbol: str = Query("GOPS-ALP", min_length=1, max_length=16),
    interval: Interval = Query("1m"),
    limit: int = Query(360, ge=1, le=5_000),
    before: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session: str = Query("regular"),
    ma: str = Query("5,20,60"),
) -> dict[str, Any]:
    windows = parse_ma_windows(ma)
    normalized_symbol = normalize_symbol(symbol)
    if session != "regular":
        return candle_response(
            normalized_symbol,
            interval,
            limit,
            before,
            from_,
            to,
            "error",
            [],
            error={"code": "UNSUPPORTED_SESSION", "message": "Only regular session is supported.", "retryable": False},
        )

    current_time = datetime.now(timezone.utc)
    lookback = max(windows, default=0)

    if from_ and to:
        start_at = parse_iso(from_)
        end_at = parse_iso(to)
        raw = candles_for_range(normalized_symbol, interval, start_at, end_at, lookback, current_time)
        attach_moving_averages(raw["withLookback"], windows)
        candles = [candle for candle in raw["withLookback"] if start_at <= parse_iso(candle["timestamp"]) < end_at]
        status = "ready"
        if len(candles) > limit:
            candles = candles[:limit]
            status = "partial"
        return candle_response(
            normalized_symbol,
            interval,
            limit,
            before,
            from_,
            to,
            status if candles else "empty",
            candles,
            has_more_after=status == "partial",
        )

    if before:
        end_bucket = floor_bucket(parse_iso(before), interval)
        raw = collect_last_candles(normalized_symbol, interval, end_bucket, limit + lookback, current_time)
        attach_moving_averages(raw, windows)
        candles = raw[-limit:]
        return candle_response(
            normalized_symbol,
            interval,
            limit,
            before,
            from_,
            to,
            "ready" if candles else "empty",
            candles,
            has_more_before=True,
        )

    latest_end = add_bucket(floor_bucket(current_time, interval), interval)
    raw = collect_last_candles(normalized_symbol, interval, latest_end, limit + lookback, current_time)
    attach_moving_averages(raw, windows)
    candles = raw[-limit:]
    return candle_response(
        normalized_symbol,
        interval,
        limit,
        before,
        from_,
        to,
        "ready" if candles else "empty",
        candles,
        has_more_before=True,
    )


@app.websocket("/ws/charts")
async def chart_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    symbol = normalize_symbol(websocket.query_params.get("symbol") or "GOPS-ALP")
    interval = normalize_interval(websocket.query_params.get("interval") or "1m")
    previous_bucket: datetime | None = None
    try:
        while True:
            live_time = mock_stream_time(datetime.now(timezone.utc))
            current_bucket = floor_bucket(live_time, interval)
            if previous_bucket and previous_bucket != current_bucket:
                closed = closed_candle_for_bucket(symbol, interval, previous_bucket)
                if closed:
                    await websocket.send_json(
                        {
                            "type": "CANDLE_CLOSED",
                            "symbol": symbol,
                            "interval": interval,
                            "data": closed,
                        }
                    )
            previous_bucket = current_bucket

            raw = collect_live_candles(symbol, interval, live_time, 80)
            attach_moving_averages(raw, [5, 20, 60])
            candle = raw[-1] if raw else None
            if candle:
                await websocket.send_json(
                    {
                        "type": "LIVE_CANDLE_UPDATE",
                        "symbol": symbol,
                        "interval": interval,
                        "data": candle,
                    }
                )
            else:
                await websocket.send_json({"type": "HEARTBEAT"})
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.upper()
    return normalized if normalized in FAKE_SYMBOL_BY_SYMBOL else FAKE_SYMBOLS[0].symbol


def candle_response(
    symbol: str,
    interval: Interval,
    limit: int,
    before: str | None,
    from_: str | None,
    to: str | None,
    status: str,
    candles: list[dict[str, Any]],
    *,
    has_more_before: bool | None = None,
    has_more_after: bool | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "request": {
            "limit": limit,
            "session": "regular",
        },
        "status": status,
        "candles": candles,
    }
    if before:
        response["request"]["before"] = before
    if from_:
        response["request"]["from"] = from_
    if to:
        response["request"]["to"] = to
    if has_more_before is not None:
        response["hasMoreBefore"] = has_more_before
    if has_more_after is not None:
        response["hasMoreAfter"] = has_more_after
    if error:
        response["error"] = error
    return response


def candles_for_range(
    symbol: str,
    interval: Interval,
    start_at: datetime,
    end_at: datetime,
    lookback: int,
    current_time: datetime,
) -> dict[str, list[dict[str, Any]]]:
    start_bucket = step_bucket(floor_bucket(start_at, interval), interval, -lookback)
    end_bucket = ceil_bucket(end_at, interval)
    candles: list[dict[str, Any]] = []
    bucket = start_bucket
    iterations = 0
    while bucket < end_bucket and iterations < MAX_RANGE_ITERATIONS:
        candle = aggregate_target_bucket(symbol, interval, bucket, current_time)
        if candle:
            candles.append(candle)
        bucket = add_bucket(bucket, interval)
        iterations += 1
    return {"withLookback": candles}


def collect_last_candles(
    symbol: str,
    interval: Interval,
    end_exclusive: datetime,
    count: int,
    current_time: datetime,
) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    bucket = subtract_bucket(end_exclusive, interval)
    max_iterations = max(MAX_RANGE_ITERATIONS, count * 12)
    iterations = 0
    while len(candles) < count and iterations < max_iterations:
        candle = aggregate_target_bucket(symbol, interval, bucket, current_time)
        if candle:
            candles.append(candle)
        bucket = subtract_bucket(bucket, interval)
        iterations += 1
    candles.reverse()
    return candles


def collect_live_candles(symbol: str, interval: Interval, live_time: datetime, count: int) -> list[dict[str, Any]]:
    current_bucket = floor_bucket(live_time, interval)
    previous = collect_last_candles(symbol, interval, current_bucket, max(0, count - 1), live_time)
    current = aggregate_live_target_bucket(symbol, interval, current_bucket, live_time)
    if current:
        previous.append(current)
    return previous[-count:]


def closed_candle_for_bucket(symbol: str, interval: Interval, bucket: datetime) -> dict[str, Any] | None:
    return aggregate_target_bucket(symbol, interval, bucket, add_bucket(bucket, interval))


def aggregate_live_target_bucket(symbol: str, interval: Interval, bucket: datetime, live_time: datetime) -> dict[str, Any] | None:
    if interval == "1m":
        return live_source_minute_candle(symbol, bucket, live_time)
    if interval == "5m" or interval == "10m":
        return aggregate_live_source_range(symbol, bucket, add_bucket(bucket, interval), bucket, live_time)
    if interval == "1D":
        return aggregate_live_day(symbol, bucket, live_time)
    if interval == "1W":
        values = [aggregate_live_day(symbol, bucket + timedelta(days=offset), live_time) for offset in range(7)]
        return aggregate_candles(bucket, add_bucket(bucket, interval), values, live_time)

    values = []
    day = bucket
    end = add_bucket(bucket, "1M")
    while day < end:
        values.append(aggregate_live_day(symbol, day, live_time))
        day += timedelta(days=1)
    return aggregate_candles(bucket, end, values, live_time)


def aggregate_live_day(symbol: str, bucket: datetime, live_time: datetime) -> dict[str, Any] | None:
    day_start = bucket.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    session_day = day_start.date()
    if not is_trading_day(session_day):
        return None
    live_day = live_time.astimezone(timezone.utc).date()
    bucket_end = day_start + timedelta(days=1)
    if session_day < live_day:
        values = daily_ohlcv(symbol, session_day.toordinal())
        return candle_from_values(day_start, bucket_end, values, bucket_end) if values else None
    if session_day > live_day:
        return None
    return aggregate_values(day_start, bucket_end, live_regular_session_source_values(symbol, session_day, live_time), live_time)


def live_regular_session_source_values(symbol: str, session_day: date, live_time: datetime) -> list[Ohlcv]:
    source_values: list[Ohlcv] = []
    local_start = datetime.combine(session_day, REGULAR_SESSION_START, tzinfo=MARKET_TIMEZONE)
    current_minute = floor_bucket(live_time, "1m")
    for minute_index in range(REGULAR_SESSION_MINUTES):
        minute = (local_start + timedelta(minutes=minute_index)).astimezone(timezone.utc)
        if minute > current_minute:
            break
        source_values.append(live_source_minute_values(symbol, minute, live_time))
    return source_values


def aggregate_live_source_range(
    symbol: str,
    source_start: datetime,
    source_end: datetime,
    target_timestamp: datetime,
    live_time: datetime,
) -> dict[str, Any] | None:
    source_values: list[Ohlcv] = []
    minute = source_start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    current_minute = floor_bucket(live_time, "1m")
    while minute < source_end and minute <= current_minute:
        if is_trading_minute(minute):
            source_values.append(live_source_minute_values(symbol, minute, live_time))
        minute += timedelta(minutes=1)
    return aggregate_values(target_timestamp, source_end, source_values, live_time)


def aggregate_target_bucket(symbol: str, interval: Interval, bucket: datetime, current_time: datetime) -> dict[str, Any] | None:
    if interval == "1m":
        return source_minute_candle(symbol, bucket, current_time)
    if interval == "5m" or interval == "10m":
        return aggregate_source_range(symbol, bucket, add_bucket(bucket, interval), bucket, current_time)
    if interval == "1D":
        return aggregate_day(symbol, bucket, current_time)
    if interval == "1W":
        values = [aggregate_day(symbol, bucket + timedelta(days=offset), current_time) for offset in range(7)]
        return aggregate_candles(bucket, add_bucket(bucket, interval), values, current_time)

    values = []
    day = bucket
    end = add_bucket(bucket, "1M")
    while day < end:
        values.append(aggregate_day(symbol, day, current_time))
        day += timedelta(days=1)
    return aggregate_candles(bucket, end, values, current_time)


def aggregate_day(symbol: str, bucket: datetime, current_time: datetime) -> dict[str, Any] | None:
    day_start = bucket.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if not is_trading_day(day_start.date()):
        return None
    bucket_end = day_start + timedelta(days=1)
    if bucket_end <= floor_bucket(current_time, "1m"):
        values = daily_ohlcv(symbol, day_start.date().toordinal())
        return candle_from_values(day_start, bucket_end, values, current_time) if values else None
    source_values = regular_session_source_values(symbol, day_start.date(), current_time)
    return aggregate_values(day_start, bucket_end, source_values, current_time)


@lru_cache(maxsize=20_000)
def daily_ohlcv(symbol: str, day_ordinal: int) -> Ohlcv | None:
    session_day = date.fromordinal(day_ordinal)
    if not is_trading_day(session_day):
        return None
    return combine_ohlcv(regular_session_source_values(symbol, session_day, datetime.max.replace(tzinfo=timezone.utc)))


def regular_session_source_values(symbol: str, session_day: date, current_time: datetime) -> list[Ohlcv]:
    source_values: list[Ohlcv] = []
    local_start = datetime.combine(session_day, REGULAR_SESSION_START, tzinfo=MARKET_TIMEZONE)
    current_minute = floor_bucket(current_time, "1m")
    for minute_index in range(REGULAR_SESSION_MINUTES):
        minute = (local_start + timedelta(minutes=minute_index)).astimezone(timezone.utc)
        if minute > current_minute:
            break
        source_values.append(source_minute_values(symbol, minute_epoch(minute)))
    return source_values


def aggregate_source_range(
    symbol: str,
    source_start: datetime,
    source_end: datetime,
    target_timestamp: datetime,
    current_time: datetime,
) -> dict[str, Any] | None:
    source_values: list[Ohlcv] = []
    minute = source_start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    current_minute = floor_bucket(current_time, "1m")
    while minute < source_end and minute <= current_minute:
        if is_trading_minute(minute):
            source_values.append(source_minute_values(symbol, minute_epoch(minute)))
        minute += timedelta(minutes=1)
    return aggregate_values(target_timestamp, source_end, source_values, current_time)


def source_minute_candle(symbol: str, bucket: datetime, current_time: datetime) -> dict[str, Any] | None:
    if not is_trading_minute(bucket) or bucket > floor_bucket(current_time, "1m"):
        return None
    return candle_from_values(bucket, add_bucket(bucket, "1m"), source_minute_values(symbol, minute_epoch(bucket)), current_time)


def live_source_minute_candle(symbol: str, bucket: datetime, live_time: datetime) -> dict[str, Any] | None:
    if not is_trading_minute(bucket) or bucket > floor_bucket(live_time, "1m"):
        return None
    return candle_from_values(bucket, add_bucket(bucket, "1m"), live_source_minute_values(symbol, bucket, live_time), live_time)


def live_source_minute_values(symbol: str, minute: datetime, live_time: datetime) -> Ohlcv:
    current_minute = floor_bucket(live_time, "1m")
    if minute < current_minute:
        return source_minute_values(symbol, minute_epoch(minute))
    if minute != current_minute:
        raise ValueError("live_source_minute_values called outside live minute range")

    normalized_symbol = normalize_symbol(symbol)
    profile = FAKE_SYMBOL_BY_SYMBOL[normalized_symbol]
    full = source_minute_values(normalized_symbol, minute_epoch(minute))
    open_price, full_high, full_low, full_close, full_volume = full
    session_day = minute.astimezone(MARKET_TIMEZONE).date()
    minute_index = regular_session_minute_index(minute)
    if minute_index is None:
        raise ValueError("live_source_minute_values called outside regular session")

    elapsed_seconds = int(clamp((live_time - minute).total_seconds(), 0, 59))
    closes = [
        live_close_at_second(profile, session_day, minute_index, full, second)
        for second in range(elapsed_seconds + 1)
    ]
    current_close = closes[-1]
    progress = (elapsed_seconds + 1) / 60
    high_reach = min(1.0, progress * (0.78 + deterministic_float(normalized_symbol, session_day.isoformat(), minute_index, "live-high-reach") * 0.58))
    low_reach = min(1.0, progress * (0.74 + deterministic_float(normalized_symbol, session_day.isoformat(), minute_index, "live-low-reach") * 0.62))
    high = max(open_price, current_close, max(closes), open_price + (full_high - open_price) * high_reach)
    low = min(open_price, current_close, min(closes), open_price - (open_price - full_low) * low_reach)
    volume = max(1, min(full_volume, int(full_volume * (progress**0.82))))
    return (
        round(open_price, 4),
        round(high, 4),
        round(max(0.01, low), 4),
        round(current_close, 4),
        volume,
    )


def live_close_at_second(
    profile: FakeSymbolProfile,
    session_day: date,
    minute_index: int,
    full: Ohlcv,
    second: int,
) -> float:
    open_price, full_high, full_low, full_close, _ = full
    progress = clamp((second + 1) / 60, 0.01, 1.0)
    eased = progress * progress * (3 - 2 * progress)
    body = open_price + (full_close - open_price) * eased
    range_size = max(0.01, full_high - full_low)
    phase = (stable_int(profile.symbol, session_day.isoformat(), minute_index, "live-phase") % 628) / 100
    tempo = 1.4 + deterministic_float(profile.symbol, session_day.isoformat(), minute_index, "live-tempo") * 2.2
    amplitude = min(range_size * 0.24, profile.volatility * 0.58) * math.sin(progress * math.pi)
    wave = math.sin(progress * math.tau * tempo + phase) * amplitude
    step_noise = (deterministic_float(profile.symbol, session_day.isoformat(), minute_index, second // 5, "live-noise") - 0.5) * amplitude * 0.34
    return clamp(body + wave + step_noise, full_low, full_high)


@lru_cache(maxsize=500_000)
def source_minute_values(symbol: str, epoch_minute: int) -> Ohlcv:
    minute = datetime.fromtimestamp(epoch_minute * 60, timezone.utc)
    minute_index = regular_session_minute_index(minute)
    if minute_index is None:
        raise ValueError("source_minute_values called outside regular session")

    profile = FAKE_SYMBOL_BY_SYMBOL[normalize_symbol(symbol)]
    session_day = minute.astimezone(MARKET_TIMEZONE).date()
    open_price = session_price_point(profile, session_day, minute_index)
    close_price = session_price_point(profile, session_day, minute_index + 1)
    micro_range = intraminute_range(profile, session_day, minute_index, open_price, close_price)
    upper_wick = micro_range * (0.42 + deterministic_float(symbol, session_day.isoformat(), minute_index, "upper") * 1.45)
    lower_wick = micro_range * (0.38 + deterministic_float(symbol, session_day.isoformat(), minute_index, "lower") * 1.55)
    high = max(open_price, close_price) + upper_wick
    low = max(0.01, min(open_price, close_price) - lower_wick)
    volume = session_volume(profile, session_day, minute_index)
    return (
        round(open_price, 4),
        round(high, 4),
        round(low, 4),
        round(close_price, 4),
        volume,
    )


@lru_cache(maxsize=900_000)
def session_price_point(profile: FakeSymbolProfile, session_day: date, point_index: int) -> float:
    shape = session_shape(profile.symbol, session_day.toordinal())
    progress = max(0, min(REGULAR_SESSION_MINUTES, point_index)) / REGULAR_SESSION_MINUTES

    session_open = profile.base_price + shape.long_trend + shape.gap + shape.day_noise * profile.volatility * 2.7
    session_drift = shape.direction * profile.volatility * (1.65 + abs(shape.direction) * 0.32) * (progress - 0.04)
    morning_wave = math.sin(progress * math.tau * 1.25 + shape.morning_phase) * profile.volatility * 0.42
    midday_wave = math.sin(progress * math.tau * 3.8 + shape.day_number * 0.11) * profile.volatility * 0.18
    block_walk = intraday_walk_component(profile, session_day, point_index) * profile.volatility
    minute_texture = (deterministic_float(profile.symbol, session_day.isoformat(), point_index, "texture") - 0.5) * profile.volatility * 0.18
    random_walk = (
        math.sin((point_index + shape.random_walk_phase_a) * 0.061) * profile.volatility * 0.2
        + math.sin((point_index + shape.random_walk_phase_b) * 0.137) * profile.volatility * 0.09
    )
    event = session_event_move(profile, session_day, point_index)
    return max(1.0, session_open + session_drift + morning_wave + midday_wave + block_walk + minute_texture + random_walk + event)


@lru_cache(maxsize=20_000)
def session_shape(symbol: str, day_number: int) -> SessionShape:
    profile = FAKE_SYMBOL_BY_SYMBOL[normalize_symbol(symbol)]
    event_roll = deterministic_float(profile.symbol, day_number, "event-roll")
    trend_push = clamp(profile.trend_bias / 0.045, -1.0, 1.0) * 0.48
    return SessionShape(
        day_number=day_number,
        day_noise=(deterministic_float(profile.symbol, day_number, "day-noise") - 0.5) * 2,
        direction=(deterministic_float(profile.symbol, day_number, "direction-noise") - 0.5) * 1.55
        + math.sin(day_number * 0.19 + stable_int(profile.symbol, "direction") % 91) * 0.42
        + trend_push,
        gap=(deterministic_float(profile.symbol, day_number, "gap") - 0.5) * profile.volatility * 4.4,
        long_trend=macro_price_component(profile, day_number),
        morning_phase=stable_int(profile.symbol, "morning") % 31,
        random_walk_phase_a=stable_int(profile.symbol, day_number, "rw1"),
        random_walk_phase_b=stable_int(profile.symbol, day_number, "rw2"),
        event_roll=event_roll,
        event_index=55 + stable_int(profile.symbol, day_number, "event-index") % 270,
        event_side=1 if deterministic_float(profile.symbol, day_number, "event-side") >= 0.5 else -1,
    )


def session_event_move(profile: FakeSymbolProfile, session_day: date, point_index: int) -> float:
    shape = session_shape(profile.symbol, session_day.toordinal())
    if shape.event_roll > 0.52:
        return 0.0
    distance = (point_index - shape.event_index) / (8 + shape.event_roll * 26)
    impulse = math.exp(-(distance * distance)) * profile.volatility * (1.7 + (0.52 - shape.event_roll) * 4.4)
    fade = math.exp(-max(0, point_index - shape.event_index) / 150) * profile.volatility * 0.5
    return shape.event_side * (impulse + fade)


@lru_cache(maxsize=20_000)
def macro_price_component(profile: FakeSymbolProfile, day_number: int) -> float:
    days = day_number - TREND_ANCHOR_DAY
    drift = days * profile.trend_bias
    trend_strength = min(1.0, abs(profile.trend_bias) / 0.04)
    swing_scale = 0.35 + trend_strength * 0.65
    monthly_swing = interpolated_noise(profile.symbol, day_number, 21, "monthly-swing") * profile.volatility * 5.8 * swing_scale
    quarterly_swing = interpolated_noise(profile.symbol, day_number, 63, "quarterly-swing") * profile.volatility * 8.5 * swing_scale
    yearly_swing = math.sin((day_number - TREND_ANCHOR_DAY) * 0.009 + stable_int(profile.symbol, "yearly") % 89) * profile.volatility * 3.5 * swing_scale
    return drift + monthly_swing + quarterly_swing + yearly_swing


def interpolated_noise(symbol: str, day_number: int, span_days: int, namespace: str) -> float:
    offset = day_number - TREND_ANCHOR_DAY
    anchor = math.floor(offset / span_days)
    position = (offset - anchor * span_days) / span_days
    eased = position * position * (3 - 2 * position)
    left = deterministic_float(symbol, namespace, anchor) * 2 - 1
    right = deterministic_float(symbol, namespace, anchor + 1) * 2 - 1
    return left + (right - left) * eased


@lru_cache(maxsize=900_000)
def intraday_walk_component(profile: FakeSymbolProfile, session_day: date, point_index: int) -> float:
    clamped = max(0, min(REGULAR_SESSION_MINUTES, point_index))
    block_width = 30
    block = clamped // block_width
    position = (clamped - block * block_width) / block_width
    eased = position * position * (3 - 2 * position)
    left = intraday_walk_anchor(profile.symbol, session_day, block)
    right = intraday_walk_anchor(profile.symbol, session_day, block + 1)
    open_reversion = -left * 0.28 * (1 - min(1, clamped / 80))
    return left + (right - left) * eased + open_reversion


@lru_cache(maxsize=120_000)
def intraday_walk_anchor(symbol: str, session_day: date, anchor_index: int) -> float:
    total = 0.0
    for index in range(max(0, anchor_index) + 1):
        roll = deterministic_float(symbol, session_day.isoformat(), index, "intraday-walk") - 0.5
        total += roll * 0.9
    return total


def intraminute_range(profile: FakeSymbolProfile, session_day: date, minute_index: int, open_price: float, close_price: float) -> float:
    body = abs(close_price - open_price)
    noise = deterministic_float(profile.symbol, session_day.isoformat(), minute_index, "range")
    spike = deterministic_float(profile.symbol, session_day.isoformat(), minute_index, "range-spike")
    open_close_pressure = 1 + 0.55 * abs(minute_index - REGULAR_SESSION_MINUTES / 2) / (REGULAR_SESSION_MINUTES / 2)
    spike_boost = 0 if spike > 0.14 else (0.14 - spike) * 2.6
    return max(0.015, body * (0.36 + noise * 0.5) + profile.volatility * (0.028 + noise**2 * 0.17 + spike_boost) * open_close_pressure)


def session_volume(profile: FakeSymbolProfile, session_day: date, minute_index: int) -> int:
    shape = session_shape(profile.symbol, session_day.toordinal())
    progress = minute_index / max(1, REGULAR_SESSION_MINUTES - 1)
    u_curve = 0.72 + 1.8 * (abs(progress - 0.5) * 2) ** 1.8
    day_regime = 0.58 + deterministic_float(profile.symbol, session_day.toordinal(), "volume-day-regime") * 1.55
    block = minute_index // 15
    block_left = deterministic_float(profile.symbol, session_day.isoformat(), block, "volume-block") * 1.9 + 0.28
    block_right = deterministic_float(profile.symbol, session_day.isoformat(), block + 1, "volume-block") * 1.9 + 0.28
    block_position = (minute_index - block * 15) / 15
    block_eased = block_position * block_position * (3 - 2 * block_position)
    block_regime = block_left + (block_right - block_left) * block_eased
    noise = 0.28 + deterministic_float(profile.symbol, session_day.isoformat(), minute_index, "volume") ** 1.8 * 2.45
    burst_roll = deterministic_float(profile.symbol, session_day.isoformat(), minute_index // 5, "volume-burst")
    burst_boost = 1 if burst_roll > 0.22 else 1 + (0.22 - burst_roll) * 12
    shock_roll = deterministic_float(profile.symbol, session_day.isoformat(), minute_index, "volume-shock")
    shock_boost = 1 if shock_roll > 0.045 else 1.8 + (0.045 - shock_roll) * 76
    event_boost = 1
    if shape.event_roll <= 0.52:
        event_boost += 3.2 * math.exp(-(((minute_index - shape.event_index) / 10) ** 2))
    return max(1, int(profile.volume_base * day_regime * u_curve * block_regime * noise * burst_boost * shock_boost * event_boost))


def aggregate_values(
    timestamp: datetime,
    bucket_end: datetime,
    source_values: list[Ohlcv | dict[str, Any] | None],
    current_time: datetime,
) -> dict[str, Any] | None:
    normalized = [normalize_ohlcv(value) for value in source_values if value]
    combined = combine_ohlcv(normalized)
    if not combined:
        return None
    return candle_from_values(timestamp, bucket_end, combined, current_time)


def combine_ohlcv(values: list[Ohlcv]) -> Ohlcv | None:
    if not values:
        return None
    return (
        values[0][0],
        max(value[1] for value in values),
        min(value[2] for value in values),
        values[-1][3],
        sum(value[4] for value in values),
    )


def aggregate_candles(
    timestamp: datetime,
    bucket_end: datetime,
    source_candles: list[dict[str, Any] | None],
    current_time: datetime,
) -> dict[str, Any] | None:
    values = [normalize_ohlcv(candle) for candle in source_candles if candle]
    return aggregate_values(timestamp, bucket_end, values, current_time)


def normalize_ohlcv(value: Ohlcv | dict[str, Any] | None) -> Ohlcv:
    if isinstance(value, dict):
        return (
            float(value["open"]),
            float(value["high"]),
            float(value["low"]),
            float(value["close"]),
            int(value["volume"]),
        )
    assert value is not None
    return value


def candle_from_values(timestamp: datetime, bucket_end: datetime, values: Ohlcv, current_time: datetime) -> dict[str, Any]:
    open_price, high, low, close_price, volume = values
    return {
        "timestamp": to_iso(timestamp),
        "open": round(open_price, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "close": round(close_price, 4),
        "volume": int(volume),
        "isClosed": bucket_end <= floor_bucket(current_time, "1m"),
    }


def attach_moving_averages(candles: list[dict[str, Any]], windows: list[int]) -> None:
    for window in windows:
        if window not in {5, 20, 60}:
            continue
        key = f"ma{window}"
        for index, candle in enumerate(candles):
            if index + 1 < window:
                continue
            closes = [float(item["close"]) for item in candles[index + 1 - window : index + 1]]
            candle[key] = round(sum(closes) / window, 4)


def parse_ma_windows(value: str) -> list[int]:
    windows: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            window = int(part)
        except ValueError:
            continue
        if 1 < window <= 240:
            windows.append(window)
    return windows


def parse_iso(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_interval(value: str) -> Interval:
    if value in {"1m", "5m", "10m", "1D", "1W", "1M"}:
        return value  # type: ignore[return-value]
    return "1m"


def mock_stream_time(wall_time: datetime) -> datetime:
    elapsed_seconds = max(0, int((wall_time.astimezone(timezone.utc) - MOCK_STREAM_STARTED_AT).total_seconds()))
    return advance_regular_session_time(mock_stream_base_time(), elapsed_seconds)


@lru_cache(maxsize=1)
def mock_stream_base_time() -> datetime:
    return next_regular_session_time(MOCK_STREAM_STARTED_AT)


def next_regular_session_time(value: datetime) -> datetime:
    local = value.astimezone(MARKET_TIMEZONE).replace(microsecond=0)
    if is_trading_day(local.date()):
        start = regular_session_start(local.date())
        end = regular_session_end(local.date())
        if local < start:
            return start.astimezone(timezone.utc)
        if start <= local < end:
            return local.astimezone(timezone.utc)

    day = local.date() + timedelta(days=1)
    for _ in range(10):
        if is_trading_day(day):
            return regular_session_start(day).astimezone(timezone.utc)
        day += timedelta(days=1)
    return regular_session_start(local.date()).astimezone(timezone.utc)


def advance_regular_session_time(base_time: datetime, seconds: int) -> datetime:
    current = base_time.astimezone(MARKET_TIMEZONE).replace(microsecond=0)
    remaining = max(0, seconds)
    while True:
        end = regular_session_end(current.date())
        available = max(0, int((end - current).total_seconds()))
        if remaining < available:
            return (current + timedelta(seconds=remaining)).astimezone(timezone.utc)
        remaining -= available
        day = current.date() + timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
        current = regular_session_start(day)


def regular_session_start(session_day: date) -> datetime:
    return datetime.combine(session_day, REGULAR_SESSION_START, tzinfo=MARKET_TIMEZONE)


def regular_session_end(session_day: date) -> datetime:
    return regular_session_start(session_day) + timedelta(minutes=REGULAR_SESSION_MINUTES)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def floor_bucket(value: datetime, interval: Interval) -> datetime:
    dt = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if interval == "1m":
        return dt
    if interval == "5m":
        return dt.replace(minute=(dt.minute // 5) * 5)
    if interval == "10m":
        return dt.replace(minute=(dt.minute // 10) * 10)
    if interval == "1D":
        return dt.replace(hour=0, minute=0)
    if interval == "1W":
        start = dt.replace(hour=0, minute=0)
        return start - timedelta(days=start.weekday())
    return dt.replace(day=1, hour=0, minute=0)


def ceil_bucket(value: datetime, interval: Interval) -> datetime:
    floored = floor_bucket(value, interval)
    normalized = value.astimezone(timezone.utc)
    if floored == normalized:
        return floored
    return add_bucket(floored, interval)


def add_bucket(value: datetime, interval: Interval) -> datetime:
    if interval == "1M":
        month = value.month + 1
        year = value.year
        if month > 12:
            month = 1
            year += 1
        return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return value + timedelta(seconds=INTERVAL_SECONDS[interval])


def step_bucket(value: datetime, interval: Interval, steps: int) -> datetime:
    bucket = value
    if steps >= 0:
        for _ in range(steps):
            bucket = add_bucket(bucket, interval)
        return bucket
    for _ in range(abs(steps)):
        bucket = subtract_bucket(bucket, interval)
    return bucket


def subtract_bucket(value: datetime, interval: Interval) -> datetime:
    if interval == "1M":
        month = value.month - 1
        year = value.year
        if month < 1:
            month = 12
            year -= 1
        return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return value - timedelta(seconds=INTERVAL_SECONDS[interval])


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5


def is_trading_minute(value: datetime) -> bool:
    return regular_session_minute_index(value) is not None


def regular_session_minute_index(value: datetime) -> int | None:
    local = value.astimezone(MARKET_TIMEZONE)
    if not is_trading_day(local.date()):
        return None
    start = local.replace(
        hour=REGULAR_SESSION_START.hour,
        minute=REGULAR_SESSION_START.minute,
        second=0,
        microsecond=0,
    )
    delta = local - start
    minutes = int(delta.total_seconds() // 60)
    if 0 <= minutes < REGULAR_SESSION_MINUTES:
        return minutes
    return None


def minute_epoch(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() // 60)


def stable_int(*parts: object) -> int:
    key = "|".join(str(part) for part in parts).encode("utf-8")
    high = zlib.crc32(key)
    low = zlib.crc32(key, 0xA5A5A5A5)
    return (high << 32) | low


def deterministic_float(*parts: object) -> float:
    return stable_int(*parts) / 2**64
