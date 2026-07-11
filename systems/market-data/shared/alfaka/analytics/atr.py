from __future__ import annotations

import math
import statistics
from typing import Any


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        if previous_close is None:
            value = high - low
        else:
            value = max(high - low, abs(high - previous_close), abs(low - previous_close))
        ranges.append(max(0.0, value))
        previous_close = float(candle["close"])
    return ranges


def atr_series(candles: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    """Return Wilder ATR without looking beyond each candle."""
    ranges = true_ranges(candles)
    result: list[float | None] = [None] * len(ranges)
    if not ranges:
        return result
    seed_length = min(period, len(ranges))
    for index in range(seed_length - 1):
        result[index] = statistics.median(ranges[:index + 1])
    seed = sum(ranges[:seed_length]) / seed_length
    result[seed_length - 1] = seed
    previous = seed
    for index in range(seed_length, len(ranges)):
        previous = ((previous * (period - 1)) + ranges[index]) / period
        result[index] = previous
    return result


def atr_quality_flags(candles: list[dict[str, Any]], period: int = 14) -> list[str]:
    ranges = true_ranges(candles)
    values = atr_series(candles, period)
    for index in range(1, len(ranges)):
        previous = values[index - 1]
        if previous and previous > 0 and ranges[index] > 12 * previous:
            return ["abnormal_true_range"]
    return []


def latest_atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    values = atr_series(candles, period)
    for value in reversed(values):
        if value is not None and math.isfinite(value):
            return max(value, 0.01)
    if not candles:
        return 0.01
    candle = candles[-1]
    return max(float(candle["high"]) - float(candle["low"]), abs(float(candle["close"])) * 0.001, 0.01)


def percentile_rank(values: list[float | None], current: float | None) -> float:
    numeric = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if current is None or not numeric:
        return 0.0
    return sum(value <= current for value in numeric) / len(numeric)
