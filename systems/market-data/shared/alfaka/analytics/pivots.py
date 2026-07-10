from __future__ import annotations

from typing import Any

from .atr import atr_series, latest_atr


def compute_pivots(
    candles: list[dict[str, Any]],
    *,
    display_from: str,
) -> list[dict[str, Any]]:
    if len(candles) < 3:
        return []

    atr_values = atr_series(candles)
    fallback_atr = latest_atr(candles)
    high_index = low_index = 0
    high_price = float(candles[0]["high"])
    low_price = float(candles[0]["low"])
    seeking: str | None = None
    raw: list[dict[str, Any]] = []

    for index in range(1, len(candles)):
        candle = candles[index]
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        atr = atr_values[index] or fallback_atr
        reversal = max(2.0 * atr, 0.015 * abs(close))

        if seeking in {None, "H"} and high >= high_price:
            high_price, high_index = high, index
        if seeking in {None, "L"} and low <= low_price:
            low_price, low_index = low, index

        if seeking is None:
            if high_price - low >= reversal and high_index < index:
                raw.append(_pivot("H", high_index, high_price, index, high_price - low, atr, candles, display_from))
                seeking = "L"
                low_price, low_index = low, index
            elif high - low_price >= reversal and low_index < index:
                raw.append(_pivot("L", low_index, low_price, index, high - low_price, atr, candles, display_from))
                seeking = "H"
                high_price, high_index = high, index
            continue

        if seeking == "L" and high - low_price >= reversal and low_index < index:
            raw.append(_pivot("L", low_index, low_price, index, high - low_price, atr, candles, display_from))
            seeking = "H"
            high_price, high_index = high, index
        elif seeking == "H" and high_price - low >= reversal and high_index < index:
            raw.append(_pivot("H", high_index, high_price, index, high_price - low, atr, candles, display_from))
            seeking = "L"
            low_price, low_index = low, index

    raw.sort(key=lambda item: (item["timestamp"], item["kind"]))
    for index, pivot in enumerate(raw, start=1):
        pivot["id"] = f"p{index}"
    return raw


def _pivot(
    kind: str,
    pivot_index: int,
    price: float,
    confirmed_index: int,
    retracement: float,
    atr: float,
    candles: list[dict[str, Any]],
    display_from: str,
) -> dict[str, Any]:
    timestamp = candles[pivot_index]["timestamp"]
    return {
        "id": "",
        "timestamp": timestamp,
        "price": round(price, 2),
        "kind": kind,
        "strength": round(min(1.0, retracement / max(3.0 * atr, 0.01)), 4),
        "confirmedAt": candles[confirmed_index]["timestamp"],
        "inDisplayWindow": timestamp >= display_from,
    }
