from __future__ import annotations

import statistics
from typing import Any


def compute_events(
    candles: list[dict[str, Any]],
    levels: list[dict[str, Any]],
    *,
    atr: float,
    display_from: str,
    interval: str,
) -> list[dict[str, Any]]:
    display = [candle for candle in candles if candle["timestamp"] >= display_from]
    if not display:
        return []
    scored_levels = [level for level in levels if float(level["score"]) >= 0.5]
    volume_z = _rolling_z([float(item.get("volume") or 0) for item in candles], 20)
    index_by_timestamp = {item["timestamp"]: index for index, item in enumerate(candles)}
    lookback_count = min(252, len(candles)) if interval == "1D" else len(candles)
    events: list[dict[str, Any]] = []
    active_breakouts: dict[str, tuple[int, str]] = {}
    for candle in display:
        index = index_by_timestamp[candle["timestamp"]]
        previous = candles[index - 1] if index > 0 else None
        close = float(candle["close"])
        zscore = volume_z[index]
        if previous:
            previous_close = float(previous["close"])
            for level in scored_levels:
                price = float(level["price"])
                direction = "up" if previous_close <= price < close else "down" if previous_close >= price > close else None
                if direction and zscore >= 1.5:
                    events.append(_event(candle, "breakout", close, [level["id"]], {"volumeZ": round(zscore, 2), "direction": direction}))
                    active_breakouts[level["id"]] = (index, direction)
                active = active_breakouts.get(level["id"])
                if active and index > active[0] and abs(close - price) <= 0.5 * atr:
                    held = close >= price if active[1] == "up" else close <= price
                    if held:
                        events.append(_event(candle, "retest", close, [level["id"]], {"direction": active[1]}))
                        active_breakouts.pop(level["id"], None)
            if abs(float(candle["open"]) - previous_close) >= atr:
                direction = "up" if float(candle["open"]) > previous_close else "down"
                filled = float(candle["low"]) <= previous_close <= float(candle["high"])
                events.append(_event(candle, "gap", float(candle["open"]), [], {"direction": direction, "unfilled": not filled}))
        if zscore >= 2.5:
            events.append(_event(candle, "volumeSpike", close, [], {"volumeZ": round(zscore, 2)}))
        prior = candles[max(0, index - lookback_count):index]
        if prior and float(candle["high"]) > max(float(item["high"]) for item in prior):
            events.append(_event(candle, "52wHigh", float(candle["high"]), [], {}))
        if prior and float(candle["low"]) < min(float(item["low"]) for item in prior):
            events.append(_event(candle, "52wLow", float(candle["low"]), [], {}))

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in sorted(events, key=lambda event: (event["timestamp"], event["kind"], event["price"])):
        fingerprint = item["timestamp"], item["kind"], tuple(item["refIds"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        item["id"] = f"e{len(unique) + 1}"
        unique.append(item)
    return unique


def _event(candle: dict[str, Any], kind: str, price: float, ref_ids: list[str], detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "",
        "timestamp": candle["timestamp"],
        "kind": kind,
        "price": round(price, 2),
        "refIds": ref_ids,
        "detail": detail,
    }


def _rolling_z(values: list[float], period: int) -> list[float]:
    result: list[float] = []
    for index in range(len(values)):
        window = values[max(0, index - period + 1):index + 1]
        deviation = statistics.pstdev(window) if len(window) > 1 else 0.0
        result.append((values[index] - statistics.mean(window)) / deviation if deviation > 0 else 0.0)
    return result
