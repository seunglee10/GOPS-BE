from __future__ import annotations

import statistics
from typing import Any

from alfaka.serving.indicators import bollinger_bands, ema, macd, rsi

from .atr import atr_series, latest_atr, percentile_rank


def compute_trends(
    candles: list[dict[str, Any]],
    pivots: list[dict[str, Any]],
    *,
    display_from: str,
    atr: float,
) -> list[dict[str, Any]]:
    display_pivots = [pivot for pivot in pivots if pivot["timestamp"] >= display_from]
    candle_index = {candle["timestamp"]: index for index, candle in enumerate(candles)}
    candidates: list[dict[str, Any]] = []
    for kind, trend_kind, breach_sign in (("L", "up", -1), ("H", "down", 1)):
        same_kind = [pivot for pivot in display_pivots if pivot["kind"] == kind]
        for first, second in zip(same_kind, same_kind[1:]):
            start = candle_index[first["timestamp"]]
            end = candle_index[second["timestamp"]]
            if end <= start:
                continue
            slope = (float(second["price"]) - float(first["price"])) / (end - start)
            if trend_kind == "up" and slope <= 0:
                continue
            if trend_kind == "down" and slope >= 0:
                continue
            breached = False
            touches = 0
            for index in range(start, len(candles)):
                line = float(first["price"]) + slope * (index - start)
                distance = float(candles[index]["close"]) - line
                if abs(distance) <= 0.5 * atr:
                    touches += 1
                if index < len(candles) - 2 and breach_sign * distance > 0.3 * atr:
                    breached = True
                    break
            if not breached:
                candidates.append({
                    "id": "",
                    "kind": trend_kind,
                    "anchorPivotIds": [first["id"], second["id"]],
                    "touches": touches,
                    "slopePerBar": round(slope, 6),
                    "channelWidth": None,
                })

    lows = sorted((item for item in candidates if item["kind"] == "up"), key=_trend_rank, reverse=True)
    highs = sorted((item for item in candidates if item["kind"] == "down"), key=_trend_rank, reverse=True)
    if lows and highs:
        low_slope = abs(float(lows[0]["slopePerBar"]))
        high_slope = abs(float(highs[0]["slopePerBar"]))
        denominator = max(low_slope, high_slope, 1e-9)
        if abs(low_slope - high_slope) / denominator <= 0.2:
            base = lows[0] if lows[0]["touches"] >= highs[0]["touches"] else highs[0]
            opposite = highs[0] if base is lows[0] else lows[0]
            anchor_ids = [*base["anchorPivotIds"], opposite["anchorPivotIds"][-1]]
            pivot_map = {pivot["id"]: pivot for pivot in pivots}
            base_price = float(pivot_map[base["anchorPivotIds"][-1]]["price"])
            opposite_price = float(pivot_map[opposite["anchorPivotIds"][-1]]["price"])
            return [{
                "id": "t1",
                "kind": "channel",
                "anchorPivotIds": anchor_ids,
                "touches": int(base["touches"]) + int(opposite["touches"]),
                "slopePerBar": base["slopePerBar"],
                "channelWidth": round(abs(opposite_price - base_price), 2),
            }]

    selected = (lows[:1] + highs[:1])[:2]
    if selected:
        for index, trend in enumerate(selected, start=1):
            trend["id"] = f"t{index}"
        return selected

    display = [candle for candle in candles if candle["timestamp"] >= display_from] or candles
    window = display[max(0, len(display) - max(2, int(len(display) * 0.4))):]
    return [{
        "id": "t1",
        "kind": "range",
        "anchorPivotIds": [],
        "touches": 0,
        "slopePerBar": 0.0,
        "channelWidth": round(max(float(item["high"]) for item in window) - min(float(item["low"]) for item in window), 2),
        "rangeFrom": window[0]["timestamp"],
        "rangeTo": window[-1]["timestamp"],
        "rangeHigh": round(max(float(item["high"]) for item in window), 2),
        "rangeLow": round(min(float(item["low"]) for item in window), 2),
    }]


def compute_regime(candles: list[dict[str, Any]], trends: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(item["close"]) for item in candles]
    highs = [float(item["high"]) for item in candles]
    lows = [float(item["low"]) for item in candles]
    volumes = [float(item.get("volume") or 0) for item in candles]
    atr_values = atr_series(candles)
    atr14 = latest_atr(candles)
    ema20 = ema(closes, 20)
    valid_ema = [value for value in ema20 if value is not None]
    ema_slope = ((valid_ema[-1] - valid_ema[-6]) / 5 / atr14) if len(valid_ema) >= 6 else 0.0
    bands = bollinger_bands(closes, 20, 2.0)
    bandwidths = [
        ((upper - lower) / middle) if middle not in {None, 0} and upper is not None and lower is not None else None
        for middle, upper, lower in bands
    ]
    bandwidth_current = next((value for value in reversed(bandwidths) if value is not None), None)
    bandwidth_percentile = percentile_rank(bandwidths, bandwidth_current)
    macd_values = macd(closes, 12, 26, 9)
    macd_state = _macd_state(macd_values, closes)
    rsi_values = rsi(closes, 14)
    rsi14 = next((value for value in reversed(rsi_values) if value is not None), 50.0)
    volume_z = _zscore_last(volumes)
    lookback = candles[-min(252, len(candles)):]
    high52 = max(float(item["high"]) for item in lookback)
    low52 = min(float(item["low"]) for item in lookback)
    last_close = closes[-1]
    trend_kind = "range"
    if any(item["kind"] in {"up", "channel"} and float(item.get("slopePerBar") or 0) > 0 for item in trends):
        trend_kind = "up"
    elif any(item["kind"] in {"down", "channel"} and float(item.get("slopePerBar") or 0) < 0 for item in trends):
        trend_kind = "down"
    elif ema_slope > 0.12:
        trend_kind = "up"
    elif ema_slope < -0.12:
        trend_kind = "down"
    return {
        "trend": trend_kind,
        "emaSlope20": round(ema_slope, 4),
        "atr14": round(atr14, 2),
        "atrPercentile": round(percentile_rank(atr_values, atr14), 4),
        "bbSqueeze": bandwidth_percentile < 0.2,
        "bbBandwidthPercentile": round(bandwidth_percentile, 4),
        "macdState": macd_state,
        "rsi14": round(float(rsi14), 2),
        "volumeZLast": round(volume_z, 2),
        "high52w": round(high52, 2),
        "low52w": round(low52, 2),
        "pctFrom52wHigh": round(((last_close / high52) - 1) * 100, 2) if high52 else 0.0,
    }


def _trend_rank(item: dict[str, Any]) -> tuple[int, float]:
    return int(item["touches"]), abs(float(item["slopePerBar"]))


def _macd_state(values: list[tuple[float | None, float | None, float | None]], closes: list[float]) -> str:
    recent = values[-6:]
    for previous, current in zip(recent, recent[1:]):
        if None in previous[:2] or None in current[:2]:
            continue
        if previous[0] <= previous[1] and current[0] > current[1]:
            return "bullish_cross_recent"
        if previous[0] >= previous[1] and current[0] < current[1]:
            return "bearish_cross_recent"
    histograms = [item[2] for item in recent if item[2] is not None]
    if len(histograms) >= 3 and len(closes) >= 6:
        if (closes[-1] - closes[-6]) * (histograms[-1] - histograms[0]) < 0:
            return "diverging"
    return "neutral"


def _zscore_last(values: list[float]) -> float:
    window = values[-20:]
    if len(window) < 2:
        return 0.0
    deviation = statistics.pstdev(window)
    return (window[-1] - statistics.mean(window)) / deviation if deviation > 0 else 0.0
