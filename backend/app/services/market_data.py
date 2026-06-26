from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

DummySymbolProfile = dict[str, str | float | int]

DUMMY_SYMBOL_PROFILES: dict[str, DummySymbolProfile] = {
    "AAPL": {
        "name": "Apple Inc.",
        "market": "NASDAQ",
        "base": 136.0,
        "trend": 0.028,
        "volatility": 1.0,
        "volumeBase": 42_000,
        "volumeWave": 68_000,
        "phase": 0.2,
    },
    "MSFT": {
        "name": "Microsoft Corp.",
        "market": "NASDAQ",
        "base": 424.0,
        "trend": -0.012,
        "volatility": 1.7,
        "volumeBase": 58_000,
        "volumeWave": 74_000,
        "phase": 1.4,
    },
    "NVDA": {
        "name": "NVIDIA Corp.",
        "market": "NASDAQ",
        "base": 158.0,
        "trend": 0.052,
        "volatility": 2.4,
        "volumeBase": 92_000,
        "volumeWave": 125_000,
        "phase": 2.3,
    },
    "TSLA": {
        "name": "Tesla Inc.",
        "market": "NASDAQ",
        "base": 302.0,
        "trend": -0.036,
        "volatility": 3.1,
        "volumeBase": 78_000,
        "volumeWave": 112_000,
        "phase": 3.1,
    },
    "SPY": {
        "name": "SPDR S&P 500 ETF",
        "market": "NYSEARCA",
        "base": 612.0,
        "trend": 0.016,
        "volatility": 0.82,
        "volumeBase": 120_000,
        "volumeWave": 88_000,
        "phase": 4.2,
    },
}


def supported_dummy_symbols() -> list[str]:
    return list(DUMMY_SYMBOL_PROFILES.keys())


def normalize_dummy_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized not in DUMMY_SYMBOL_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported dummy symbol '{normalized}'. Supported symbols: {', '.join(supported_dummy_symbols())}.",
        )
    return normalized


def dummy_profile(symbol: str) -> DummySymbolProfile:
    return DUMMY_SYMBOL_PROFILES[normalize_dummy_symbol(symbol)]


def interval_to_minutes(interval: str) -> int:
    return {"1m": 1, "5m": 5, "10m": 10}.get(interval, 1)


def deterministic_base(symbol: str) -> float:
    return float(dummy_profile(symbol)["base"])


def moving_average(values: list[float], period: int, index: int) -> float | None:
    if index + 1 < period:
        return None
    window = values[index - period + 1:index + 1]
    return round(sum(window) / period, 4)


def symbol_seed(symbol: str) -> int:
    return sum((index + 1) * ord(char) for index, char in enumerate(symbol))


def hash_ratio(seed: int, index: int, salt: int) -> float:
    value = (seed + index * 0x9E3779B1 + salt * 0x85EBCA77) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value / 0xFFFFFFFF


def signed_noise(seed: int, index: int, salt: int) -> float:
    return hash_ratio(seed, index, salt) * 2 - 1


def live_shift_for_step(seed: int, volatility: float, candle_index: int, step: int) -> float:
    if step <= 0:
        return 0.0
    return signed_noise(seed, candle_index * 11 + step, 41) * 0.22 * volatility


def build_dummy_candles(
    symbol: str,
    interval: str,
    count: int,
    live_last_step: int = 0,
    force_last_closed: bool | None = None,
) -> list[dict[str, Any]]:
    symbol = normalize_dummy_symbol(symbol)
    minutes = interval_to_minutes(interval)
    start = datetime(2026, 6, 25, 13, 30, tzinfo=UTC)
    profile = dummy_profile(symbol)
    base = float(profile["base"])
    trend = float(profile["trend"])
    volatility = float(profile["volatility"])
    volume_base = int(profile["volumeBase"])
    volume_wave = int(profile["volumeWave"])
    phase = float(profile["phase"])
    seed = symbol_seed(symbol) + int(phase * 1000)
    closes: list[float] = []
    raw: list[dict[str, Any]] = []
    previous_close = base
    momentum = 0.0

    for index in range(count):
        step = live_last_step if index == count - 1 else 0
        gap = 0.0 if index == 0 else signed_noise(seed, index, 7) * 0.08 * volatility
        open_price = previous_close + gap
        mean_reversion = (base + index * trend - open_price) * 0.018
        shock = (
            signed_noise(seed, index, 11) * 0.48 +
            signed_noise(seed, index, 17) * 0.25 +
            signed_noise(seed, index // 5, 23) * 0.17
        ) * volatility
        body = trend + mean_reversion + momentum * 0.34 + shock
        close_base = open_price + body
        if step > 0:
            intra_closes = [
                close_base + live_shift_for_step(seed, volatility, index, intra_step)
                for intra_step in range(0, step + 1)
            ]
            close_price = intra_closes[-1]
            high_price = max([open_price, *intra_closes])
            low_price = min([open_price, *intra_closes])
        else:
            close_price = close_base
            upper_wick = (0.16 + hash_ratio(seed, index, 29) * 0.34) * volatility
            lower_wick = (0.16 + hash_ratio(seed, index, 31) * 0.34) * volatility
            high_price = max(open_price, close_price) + upper_wick
            low_price = min(open_price, close_price) - lower_wick
        volume_jitter = int(hash_ratio(seed, index, 37) * volume_wave)
        volume = volume_base + volume_jitter + step * int(900 * volatility)
        is_closed = index != count - 1
        if index == count - 1 and force_last_closed is not None:
            is_closed = force_last_closed

        closes.append(close_price)
        raw.append({
            "timestamp": (start + timedelta(minutes=minutes * index)).isoformat().replace("+00:00", "Z"),
            "open": round(open_price, 4),
            "high": round(high_price, 4),
            "low": round(low_price, 4),
            "close": round(close_price, 4),
            "volume": volume,
            "isClosed": is_closed,
        })
        previous_close = close_price
        momentum = momentum * 0.52 + shock * 0.48

    return [
        {
            **candle,
            **({
                "ma5": ma5,
            } if (ma5 := moving_average(closes, 5, index)) is not None else {}),
            **({
                "ma20": ma20,
            } if (ma20 := moving_average(closes, 20, index)) is not None else {}),
            **({
                "ma60": ma60,
            } if (ma60 := moving_average(closes, 60, index)) is not None else {}),
        }
        for index, candle in enumerate(raw)
    ]


def build_live_candle(symbol: str, interval: str, index: int, event_type: str, update_step: int = 0) -> dict[str, Any]:
    symbol = normalize_dummy_symbol(symbol)
    if event_type == "CANDLE_CORRECTED" and index > 1:
        candles = build_dummy_candles(symbol, interval, max(31, index), live_last_step=update_step + 2, force_last_closed=True)
        candle = dict(candles[-1])
        profile = dummy_profile(symbol)
        adjustment = 0.12 * float(profile["volatility"])
        candle["close"] = round(float(candle["close"]) + adjustment, 4)
        candle["high"] = round(max(float(candle["high"]), float(candle["close"]) + adjustment), 4)
        candle["isClosed"] = True
        return candle

    candles = build_dummy_candles(
        symbol,
        interval,
        max(31, index + 1),
        live_last_step=update_step,
        force_last_closed=event_type == "CANDLE_CLOSED",
    )
    return dict(candles[-1])


def build_symbol_summary(symbol: str) -> dict[str, Any]:
    symbol = normalize_dummy_symbol(symbol)
    profile = dummy_profile(symbol)
    candles = build_dummy_candles(symbol, "1m", 160)
    last = candles[-1]
    previous = candles[-21]
    previous_close = float(previous["close"])
    change_percent = ((float(last["close"]) - previous_close) / previous_close) * 100
    return {
        "symbol": symbol,
        "name": profile["name"],
        "market": profile["market"],
        "lastPrice": last["close"],
        "changePercent": round(change_percent, 2),
        "volume": last["volume"],
    }
