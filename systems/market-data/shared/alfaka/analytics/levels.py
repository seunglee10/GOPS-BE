from __future__ import annotations

import math
from typing import Any


def compute_levels(
    candles: list[dict[str, Any]],
    pivots: list[dict[str, Any]],
    *,
    atr: float,
    volume_profile: dict[str, Any],
    expected_bars: int,
) -> list[dict[str, Any]]:
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda item: (float(item["price"]), item["timestamp"]))
    clusters: list[list[dict[str, Any]]] = []
    for pivot in ordered:
        if not clusters:
            clusters.append([pivot])
            continue
        previous_price = _weighted_price(clusters[-1])
        threshold = max(0.5 * atr, 0.004 * max(abs(previous_price), abs(float(pivot["price"]))))
        if abs(float(pivot["price"]) - previous_price) <= threshold:
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])

    timestamp_index = {candle["timestamp"]: index for index, candle in enumerate(candles)}
    vp_prices = _volume_profile_prices(volume_profile)
    levels: list[dict[str, Any]] = []
    sample_penalty = max(0.0, (0.6 - (len(candles) / max(1, expected_bars))) * 0.25)
    for cluster in clusters:
        price = round(_weighted_price(cluster), 2)
        touches = len(cluster)
        latest_index = max(timestamp_index.get(item["timestamp"], 0) for item in cluster)
        age = max(0, len(candles) - 1 - latest_index)
        half_life = max(1.0, len(candles) / 4)
        recency = math.exp(-math.log(2) * age / half_life)
        reaction_values = []
        for pivot in cluster:
            index = timestamp_index.get(pivot["timestamp"], 0)
            later = candles[index + 1:index + 6]
            if later:
                reaction_values.append(max(abs(float(item["close"]) - price) for item in later) / max(atr, 0.01))
        reaction = min(1.0, (sum(reaction_values) / len(reaction_values)) / 3) if reaction_values else 0.0
        vp_confluence = any(abs(price - candidate) <= 0.5 * atr for candidate in vp_prices)
        role_flips = _role_flips(candles, price, atr)
        round_number = _is_round_number(price)
        touch_norm = min(1.0, max(0.0, (touches - 2) / 4))
        score = (
            0.30 * touch_norm
            + 0.20 * recency
            + 0.15 * reaction
            + 0.20 * float(vp_confluence)
            + 0.10 * float(role_flips > 0)
            + 0.05 * float(round_number)
            - sample_penalty
        )
        levels.append({
            "id": "",
            "price": price,
            "score": round(min(1.0, max(0.0, score)), 4),
            "touches": touches,
            "lastTestAt": max(item["timestamp"] for item in cluster),
            "roleFlips": role_flips,
            "vpConfluence": vp_confluence,
            "roundNumber": round_number,
            "memberPivotIds": [item["id"] for item in sorted(cluster, key=lambda item: item["timestamp"])],
        })
    levels.sort(key=lambda item: (-item["score"], item["price"]))
    for index, level in enumerate(levels, start=1):
        level["id"] = f"l{index}"
    return levels


def _weighted_price(cluster: list[dict[str, Any]]) -> float:
    weights = [max(0.01, float(item.get("strength") or 0.0)) for item in cluster]
    return sum(float(item["price"]) * weight for item, weight in zip(cluster, weights)) / sum(weights)


def _volume_profile_prices(payload: dict[str, Any]) -> list[float]:
    prices: list[float] = []
    poc = payload.get("poc") or {}
    if poc.get("priceMid") is not None:
        prices.append(float(poc["priceMid"]))
    value_area = payload.get("valueArea") or {}
    for key in ("low", "high"):
        if value_area.get(key) is not None:
            prices.append(float(value_area[key]))
    bins = sorted(payload.get("bins") or [], key=lambda item: float(item.get("volume") or 0), reverse=True)
    prices.extend(float(item["priceMid"]) for item in bins[:3] if item.get("priceMid") is not None)
    return prices


def _role_flips(candles: list[dict[str, Any]], price: float, atr: float) -> int:
    sides: list[int] = []
    tolerance = 0.2 * atr
    for candle in candles:
        close = float(candle["close"])
        side = 1 if close > price + tolerance else -1 if close < price - tolerance else 0
        if side and (not sides or side != sides[-1]):
            sides.append(side)
    return max(0, len(sides) - 1)


def _is_round_number(price: float) -> bool:
    if price <= 0:
        return False
    exponent = 10 ** math.floor(math.log10(price))
    candidates = [factor * exponent for factor in (1, 2, 5, 10)]
    return any(abs(price - candidate) / price <= 0.002 for candidate in candidates)
