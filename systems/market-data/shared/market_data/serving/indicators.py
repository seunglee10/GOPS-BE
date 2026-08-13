from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from market_data.serving.time_utils import canonical_utc_timestamp, parse_utc_time


INDICATOR_CALCULATION_VERSION = "indicator-v1"
DEFAULT_INDICATOR_LAYER_IDS = ("sma:5", "sma:20", "sma:60")
LEGACY_MA_LAYER_ALIASES = {
    "ma5": "sma:5",
    "ma20": "sma:20",
    "ma60": "sma:60",
}


@dataclass(frozen=True)
class IndicatorSpec:
    id: str
    kind: str
    placement: str
    parameters: dict[str, float | int]


def indicator_specs_from_csv(value: str | Iterable[str] | None) -> list[IndicatorSpec]:
    tokens = list(indicator_layer_tokens(value))
    specs: list[IndicatorSpec] = []
    seen: set[str] = set()
    for token in tokens:
        spec = parse_indicator_spec(token)
        if spec.id in seen:
            continue
        seen.add(spec.id)
        specs.append(spec)
    return specs


def indicator_layer_tokens(value: str | Iterable[str] | None) -> Iterable[str]:
    if value is None:
        yield from DEFAULT_INDICATOR_LAYER_IDS
        return
    if isinstance(value, str):
        raw_tokens = value.split(",")
    else:
        raw_tokens = value
    emitted = False
    for item in raw_tokens:
        token = str(item).strip()
        if not token:
            continue
        emitted = True
        yield token
    if not emitted:
        yield from DEFAULT_INDICATOR_LAYER_IDS


def parse_indicator_spec(token: str) -> IndicatorSpec:
    normalized = token.strip().lower().replace("_", "-")
    normalized = LEGACY_MA_LAYER_ALIASES.get(normalized, normalized)
    if normalized.startswith("sma") and ":" not in normalized and normalized[3:].isdigit():
        normalized = f"sma:{normalized[3:]}"
    if normalized.startswith("ema") and ":" not in normalized and normalized[3:].isdigit():
        normalized = f"ema:{normalized[3:]}"
    if normalized.startswith("wma") and ":" not in normalized and normalized[3:].isdigit():
        normalized = f"wma:{normalized[3:]}"
    if normalized.startswith("atr") and ":" not in normalized and normalized[3:].isdigit():
        normalized = f"atr:{normalized[3:]}"
    parts = normalized.split(":")
    kind = parts[0]

    if kind in {"sma", "ema", "wma"}:
        period = read_int(parts, 1, default=20, minimum=1, maximum=500)
        return IndicatorSpec(id=f"{kind}:{period}", kind=kind, placement="overlay", parameters={"period": period})
    if kind in {"bollinger", "bb"}:
        period = read_int(parts, 1, default=20, minimum=1, maximum=500)
        multiplier = read_float(parts, 2, default=2.0, minimum=0.1, maximum=10.0)
        multiplier_text = format_number(multiplier)
        return IndicatorSpec(
            id=f"bollinger:{period}:{multiplier_text}",
            kind="bollinger",
            placement="overlay",
            parameters={"period": period, "multiplier": multiplier},
        )
    if kind == "rsi":
        period = read_int(parts, 1, default=14, minimum=1, maximum=500)
        return IndicatorSpec(id=f"rsi:{period}", kind="rsi", placement="below", parameters={"period": period})
    if kind == "atr":
        period = read_int(parts, 1, default=14, minimum=1, maximum=500)
        return IndicatorSpec(id=f"atr:{period}", kind="atr", placement="below", parameters={"period": period})
    if kind in {"stochastic", "stoch"}:
        k_period = read_int(parts, 1, default=14, minimum=1, maximum=500)
        smooth_k = read_int(parts, 2, default=3, minimum=1, maximum=100)
        d_period = read_int(parts, 3, default=3, minimum=1, maximum=100)
        return IndicatorSpec(
            id=f"stochastic:{k_period}:{smooth_k}:{d_period}",
            kind="stochastic",
            placement="below",
            parameters={"kPeriod": k_period, "smoothK": smooth_k, "dPeriod": d_period},
        )
    if kind == "macd":
        fast = read_int(parts, 1, default=12, minimum=1, maximum=500)
        slow = read_int(parts, 2, default=26, minimum=1, maximum=500)
        signal = read_int(parts, 3, default=9, minimum=1, maximum=100)
        if fast >= slow:
            raise ValueError("MACD fast period must be smaller than slow period.")
        return IndicatorSpec(
            id=f"macd:{fast}:{slow}:{signal}",
            kind="macd",
            placement="below",
            parameters={"fast": fast, "slow": slow, "signal": signal},
        )
    raise ValueError(f"Unsupported indicator layer: {token}")


def indicator_required_lookback_bars(specs: Iterable[IndicatorSpec]) -> int:
    required = 0
    for spec in specs:
        params = spec.parameters
        if spec.kind in {"sma", "ema", "wma"}:
            required = max(required, int(params["period"]))
        elif spec.kind == "bollinger":
            required = max(required, int(params["period"]))
        elif spec.kind == "rsi":
            required = max(required, int(params["period"]) + 1)
        elif spec.kind == "atr":
            required = max(required, int(params["period"]) + 1)
        elif spec.kind == "stochastic":
            required = max(required, int(params["kPeriod"]) + int(params["smoothK"]) + int(params["dPeriod"]))
        elif spec.kind == "macd":
            required = max(required, int(params["slow"]) + int(params["signal"]))
    return required


def compute_indicator_payload(
    candles: list[dict[str, Any]],
    specs: Iterable[IndicatorSpec],
    *,
    from_time: str | None = None,
    to_time: str | None = None,
) -> dict[str, Any]:
    normalized_candles = normalize_candles(candles)
    timestamps = [candle["timestamp"] for candle in normalized_candles]
    closes = [candle["close"] for candle in normalized_candles]
    highs = [candle["high"] for candle in normalized_candles]
    lows = [candle["low"] for candle in normalized_candles]
    indicators = []
    series: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        points = indicator_points(spec, timestamps, closes, highs, lows)
        points = filter_points(points, from_time=from_time, to_time=to_time)
        series[spec.id] = points
        indicators.append({
            "id": spec.id,
            "kind": spec.kind,
            "placement": spec.placement,
            "parameters": spec.parameters,
            "points": points,
        })
    return {
        "calculationVersion": INDICATOR_CALCULATION_VERSION,
        "indicators": indicators,
        "series": series,
    }


def normalize_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for candle in candles:
        timestamp = canonical_utc_timestamp(candle.get("timestamp"))
        if not timestamp:
            continue
        close = number_or_none(candle.get("close"))
        normalized.append({
            "timestamp": timestamp,
            "close": close,
            "high": number_or_none(candle.get("high")),
            "low": number_or_none(candle.get("low")),
        })
    normalized.sort(key=lambda item: item["timestamp"])
    return normalized


def indicator_points(
    spec: IndicatorSpec,
    timestamps: list[str],
    closes: list[float | None],
    highs: list[float | None],
    lows: list[float | None],
) -> list[dict[str, Any]]:
    if spec.kind == "sma":
        values = rolling_sma(closes, int(spec.parameters["period"]))
        return value_points(timestamps, values)
    if spec.kind == "ema":
        values = ema(closes, int(spec.parameters["period"]))
        return value_points(timestamps, values)
    if spec.kind == "wma":
        values = rolling_wma(closes, int(spec.parameters["period"]))
        return value_points(timestamps, values)
    if spec.kind == "bollinger":
        values = bollinger_bands(closes, int(spec.parameters["period"]), float(spec.parameters["multiplier"]))
        return [
            {"timestamp": timestamp, "middle": middle, "upper": upper, "lower": lower}
            for timestamp, (middle, upper, lower) in zip(timestamps, values)
        ]
    if spec.kind == "rsi":
        values = rsi(closes, int(spec.parameters["period"]))
        return value_points(timestamps, values)
    if spec.kind == "atr":
        values = atr(highs, lows, closes, int(spec.parameters["period"]))
        return value_points(timestamps, values)
    if spec.kind == "stochastic":
        values = stochastic(
            highs,
            lows,
            closes,
            int(spec.parameters["kPeriod"]),
            int(spec.parameters["smoothK"]),
            int(spec.parameters["dPeriod"]),
        )
        return [{"timestamp": timestamp, "k": k_value, "d": d_value} for timestamp, (k_value, d_value) in zip(timestamps, values)]
    if spec.kind == "macd":
        values = macd(
            closes,
            int(spec.parameters["fast"]),
            int(spec.parameters["slow"]),
            int(spec.parameters["signal"]),
        )
        return [
            {"timestamp": timestamp, "macd": line, "signal": signal, "histogram": histogram}
            for timestamp, (line, signal, histogram) in zip(timestamps, values)
        ]
    raise ValueError(f"Unsupported indicator kind: {spec.kind}")


def value_points(timestamps: list[str], values: list[float | None]) -> list[dict[str, Any]]:
    return [{"timestamp": timestamp, "value": value} for timestamp, value in zip(timestamps, values)]


def filter_points(points: list[dict[str, Any]], *, from_time: str | None, to_time: str | None) -> list[dict[str, Any]]:
    start = parse_utc_time(from_time)
    end = parse_utc_time(to_time)
    if not start and not end:
        return points
    filtered = []
    for point in points:
        timestamp = parse_utc_time(point.get("timestamp"))
        if not timestamp:
            continue
        if start and timestamp < start:
            continue
        if end and timestamp > end:
            continue
        filtered.append(point)
    return filtered


def rolling_sma(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        window = values[index - period + 1:index + 1]
        if len(window) < period or any(value is None for value in window):
            result.append(None)
            continue
        result.append(sum(value for value in window if value is not None) / period)
    return result


def rolling_wma(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = []
    weights = list(range(1, period + 1))
    divisor = sum(weights)
    for index in range(len(values)):
        window = values[index - period + 1:index + 1]
        if len(window) < period or any(value is None for value in window):
            result.append(None)
            continue
        result.append(sum(value * weight for value, weight in zip(window, weights) if value is not None) / divisor)
    return result


def ema(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    seed: list[float] = []
    previous: float | None = None
    multiplier = 2 / (period + 1)
    for index, value in enumerate(values):
        if value is None:
            continue
        if previous is None:
            seed.append(value)
            if len(seed) == period:
                previous = sum(seed) / period
                result[index] = previous
            continue
        previous = (value - previous) * multiplier + previous
        result[index] = previous
    return result


def bollinger_bands(values: list[float | None], period: int, multiplier: float) -> list[tuple[float | None, float | None, float | None]]:
    result: list[tuple[float | None, float | None, float | None]] = []
    for index in range(len(values)):
        window = values[index - period + 1:index + 1]
        if len(window) < period or any(value is None for value in window):
            result.append((None, None, None))
            continue
        numeric = [value for value in window if value is not None]
        middle = sum(numeric) / period
        deviation = math.sqrt(sum((value - middle) ** 2 for value in numeric) / period)
        result.append((middle, middle + multiplier * deviation, middle - multiplier * deviation))
    return result


def rsi(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    initial_changes = price_changes(values, 1, period)
    if initial_changes is None:
        return result
    average_gain = sum(max(change, 0) for change in initial_changes) / period
    average_loss = sum(max(-change, 0) for change in initial_changes) / period
    result[period] = rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        if values[index] is None or values[index - 1] is None:
            continue
        change = values[index] - values[index - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        result[index] = rsi_value(average_gain, average_loss)
    return result


def atr(
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float | None],
    period: int,
) -> list[float | None]:
    """Average True Range with Wilder smoothing.

    True range needs the previous close, so the first ATR value appears at
    index `period` (period + 1 bars of lookback).
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    true_ranges: list[float | None] = [None] * len(closes)
    for index in range(1, len(closes)):
        high = highs[index]
        low = lows[index]
        previous_close = closes[index - 1]
        if high is None or low is None or previous_close is None:
            continue
        true_ranges[index] = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
    initial_window = true_ranges[1:period + 1]
    if any(value is None for value in initial_window):
        return result
    previous_atr = sum(value for value in initial_window if value is not None) / period
    result[period] = previous_atr
    for index in range(period + 1, len(closes)):
        true_range = true_ranges[index]
        if true_range is None:
            result[index] = previous_atr
            continue
        previous_atr = (previous_atr * (period - 1) + true_range) / period
        result[index] = previous_atr
    return result


def price_changes(values: list[float | None], start: int, end: int) -> list[float] | None:
    changes = []
    for index in range(start, end + 1):
        if values[index] is None or values[index - 1] is None:
            return None
        changes.append(values[index] - values[index - 1])
    return changes


def rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def stochastic(
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float | None],
    k_period: int,
    smooth_k: int,
    d_period: int,
) -> list[tuple[float | None, float | None]]:
    raw_k: list[float | None] = []
    for index in range(len(closes)):
        high_window = highs[index - k_period + 1:index + 1]
        low_window = lows[index - k_period + 1:index + 1]
        if (
            len(high_window) < k_period
            or len(low_window) < k_period
            or closes[index] is None
            or any(value is None for value in high_window)
            or any(value is None for value in low_window)
        ):
            raw_k.append(None)
            continue
        highest = max(value for value in high_window if value is not None)
        lowest = min(value for value in low_window if value is not None)
        raw_k.append(50.0 if highest == lowest else ((closes[index] - lowest) / (highest - lowest)) * 100)
    smoothed_k = rolling_sma(raw_k, smooth_k)
    signal_d = rolling_sma(smoothed_k, d_period)
    return list(zip(smoothed_k, signal_d))


def macd(values: list[float | None], fast: int, slow: int, signal: int) -> list[tuple[float | None, float | None, float | None]]:
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line = [
        fast_value - slow_value if fast_value is not None and slow_value is not None else None
        for fast_value, slow_value in zip(fast_ema, slow_ema)
    ]
    signal_line = ema(macd_line, signal)
    return [
        (
            line,
            signal_value,
            line - signal_value if line is not None and signal_value is not None else None,
        )
        for line, signal_value in zip(macd_line, signal_line)
    ]


def read_int(parts: list[str], index: int, *, default: int, minimum: int, maximum: int) -> int:
    if index >= len(parts) or parts[index] == "":
        return default
    try:
        value = int(parts[index])
    except ValueError as exc:
        raise ValueError(f"Invalid integer parameter: {parts[index]}") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"Integer parameter must be between {minimum} and {maximum}.")
    return value


def read_float(parts: list[str], index: int, *, default: float, minimum: float, maximum: float) -> float:
    if index >= len(parts) or parts[index] == "":
        return default
    try:
        value = float(parts[index])
    except ValueError as exc:
        raise ValueError(f"Invalid numeric parameter: {parts[index]}") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"Numeric parameter must be between {minimum} and {maximum}.")
    return value


def format_number(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value).rstrip("0").rstrip(".")


def number_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
