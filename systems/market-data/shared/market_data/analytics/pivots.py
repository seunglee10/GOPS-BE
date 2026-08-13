from __future__ import annotations

import hashlib
import math
from typing import Any

from .atr import atr_series
from .config import QUALITY_CONFIG


def compute_pivots(candles: list[dict[str, Any]], *, display_from: str, interval: str = "1D") -> list[dict[str, Any]]:
    if len(candles) < 3:
        return []
    config = QUALITY_CONFIG[interval]
    atr_values = atr_series(candles)
    tactical = _directional_change(candles, atr_values, config.pivot_confirm_bars, 1.25, 0.010, "tactical")
    structural = _directional_change(candles, atr_values, config.pivot_confirm_bars, 2.0, 0.015, "structural")
    promoted = []
    for pivot in structural:
        prominence = _prominence(candles, pivot)
        atr = float(atr_values[pivot["barIndex"]] or 0)
        prominence_atr = prominence / atr if atr > 0 else 0.0
        if prominence_atr < 2.0:
            continue
        pivot["prominenceAtr"] = prominence_atr
        promoted.append(pivot)
    structural_keys = {(item["kind"], item["barIndex"]) for item in promoted}
    combined = promoted + [item for item in tactical if (item["kind"], item["barIndex"]) not in structural_keys]
    combined = _separate_same_kind(combined, config.pivot_separation)
    for pivot in combined:
        age = len(candles) - 1 - pivot["barIndex"]
        recency = math.exp(-math.log(2) * age / max(1, 0.5 * config.display_bars))
        pivot["strength"] = round(
            0.45 * min(1.0, float(pivot.get("prominenceAtr") or 0) / 4)
            + 0.35 * min(1.0, float(pivot["reversalAtr"]) / 4)
            + 0.20 * recency,
            4,
        )
        pivot["inDisplayWindow"] = pivot["timestamp"] >= display_from
        pivot["id"] = _pivot_id(interval, pivot)
        pivot.pop("_confirmedIndex", None)
    return sorted(combined, key=lambda item: (item["barIndex"], item["kind"], item["id"]))


def _directional_change(candles, atr_values, confirm_bars, atr_factor, price_factor, grade):
    high_index = low_index = 0
    high_price = float(candles[0]["high"])
    low_price = float(candles[0]["low"])
    seeking = None
    result = []
    for index in range(1, len(candles)):
        high, low = float(candles[index]["high"]), float(candles[index]["low"])
        if seeking in {None, "H"} and high >= high_price: high_price, high_index = high, index
        if seeking in {None, "L"} and low <= low_price: low_price, low_index = low, index
        high_atr = float(atr_values[high_index] or 0)
        low_atr = float(atr_values[low_index] or 0)
        high_threshold = max(atr_factor * high_atr, price_factor * high_price)
        low_threshold = max(atr_factor * low_atr, price_factor * low_price)
        if seeking in {None, "H"} and index - high_index >= confirm_bars and high_price - low >= high_threshold:
            result.append(_pivot("H", high_index, high_price, index, (high_price - low) / max(high_atr, 1e-12), candles, grade))
            seeking, low_index, low_price = "L", index, low
        elif seeking in {None, "L"} and index - low_index >= confirm_bars and high - low_price >= low_threshold:
            result.append(_pivot("L", low_index, low_price, index, (high - low_price) / max(low_atr, 1e-12), candles, grade))
            seeking, high_index, high_price = "H", index, high
    return result


def _pivot(kind, index, price, confirmed, reversal_atr, candles, grade):
    row = candles[index]
    return {
        "id": "", "timestamp": row["timestamp"], "candleKey": row.get("candleKey"),
        "barIndex": index, "price": float(price), "kind": kind, "grade": grade,
        "reversalAtr": float(reversal_atr), "prominenceAtr": 0.0,
        "confirmedAt": candles[confirmed]["timestamp"], "_confirmedIndex": confirmed,
    }


def _prominence(candles, pivot):
    index, confirmed = pivot["barIndex"], pivot["_confirmedIndex"]
    left = max(0, index - 20)
    if index <= left or confirmed <= index:
        return 0.0
    if pivot["kind"] == "H":
        return max(0.0, float(pivot["price"]) - max(min(float(row["low"]) for row in candles[left:index + 1]), min(float(row["low"]) for row in candles[index:confirmed + 1])))
    return max(0.0, min(max(float(row["high"]) for row in candles[left:index + 1]), max(float(row["high"]) for row in candles[index:confirmed + 1])) - float(pivot["price"]))


def _separate_same_kind(pivots, separation):
    selected = []
    for pivot in sorted(pivots, key=lambda item: (item["barIndex"], item["kind"], item["grade"])):
        conflict = next((item for item in reversed(selected) if item["kind"] == pivot["kind"] and pivot["barIndex"] - item["barIndex"] < separation), None)
        if conflict is None:
            selected.append(pivot)
            continue
        rank = lambda item: (float(item.get("prominenceAtr") or 0), float(item["reversalAtr"]), item["barIndex"], item["grade"] == "structural")
        if rank(pivot) > rank(conflict):
            selected[selected.index(conflict)] = pivot
    return selected


def _pivot_id(interval, pivot):
    raw = f"{interval}|{pivot['kind']}|{pivot.get('candleKey') or pivot['timestamp']}|{pivot['grade']}"
    return f"{interval}:pivot:{hashlib.sha256(raw.encode()).hexdigest()[:10]}"
