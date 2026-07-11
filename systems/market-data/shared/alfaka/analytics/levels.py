from __future__ import annotations

import hashlib
import math
import statistics
from typing import Any

from .atr import atr_series
from .config import QUALITY_CONFIG


def compute_levels(
    candles: list[dict[str, Any]], pivots: list[dict[str, Any]], *, atr: float,
    volume_profile: dict[str, Any], expected_bars: int, interval: str = "1D",
) -> list[dict[str, Any]]:
    structural = [item for item in pivots if item.get("grade") == "structural"]
    if not structural:
        return []
    atr_values = [float(value or 0) for value in atr_series(candles)]
    config = QUALITY_CONFIG[interval]
    clusters = _bounded_clusters(structural, atr_values)
    vp_prices = _volume_profile_prices(volume_profile)
    levels = []
    for cluster in clusters:
        pivot_atrs = [atr_values[item["barIndex"]] for item in cluster if atr_values[item["barIndex"]] > 0]
        if not pivot_atrs:
            continue
        median_atr = statistics.median(pivot_atrs)
        center = _weighted_median([(float(item["price"]), 0.25 + 0.75 * float(item.get("strength") or 0)) for item in cluster])
        mad = _weighted_median([(abs(float(item["price"]) - center), 0.25 + 0.75 * float(item.get("strength") or 0)) for item in cluster])
        half_width_atr = min(0.40, max(0.075, 1.4826 * mad / median_atr))
        low, high = center - half_width_atr * median_atr, center + half_width_atr * median_atr
        episodes = _touch_episodes(candles, low, high, atr_values, config)
        state = _role_state(candles, low, high, atr_values, episodes, config)
        valid = [item for item in episodes if item["outcome"] == "reaction"]
        if not episodes:
            continue
        last_touch = episodes[-1]["startIndex"]
        age = len(candles) - 1 - last_touch
        distance_atr = _zone_distance(float(candles[-1]["close"]), low, high) / max(float(atr_values[-1]), 1e-12)
        reaction_values = [max(0.0, min(1.0, (item["mfeAtr"] - item["maeAtr"]) / 2)) for item in valid]
        touch_quality = 0.5 * min(1.0, max(0.0, (len(episodes) - 1) / 3)) + 0.5 * (statistics.median([min(1.0, item["mfeAtr"] / 2) for item in valid]) if valid else 0)
        recency = math.exp(-math.log(2) * age / config.level_half_life)
        relevance = 1 - min(1.0, distance_atr / 4)
        vp = any(abs(center - value) <= 0.5 * median_atr for value in vp_prices)
        score = 0.30 * touch_quality + 0.20 * recency + 0.15 * (statistics.median(reaction_values) if reaction_values else 0) + 0.15 * relevance + 0.15 * float(vp) + 0.05 * float(state.startswith("role_flip"))
        hard_pass = state not in {"unresolved", "invalidated", "break_up_pending", "break_down_pending"} and len(episodes) >= 2 and age <= config.level_last_touch_max_age and distance_atr <= 4
        local_id = hashlib.sha256(f"{interval}|{','.join(sorted(item['id'] for item in cluster))}".encode()).hexdigest()[:10]
        levels.append({
            "id": f"{interval}:level:{local_id}", "price": round(center, 2),
            "zoneLow": round(low, 4), "zoneHigh": round(high, 4), "halfWidthAtr": round(half_width_atr, 4),
            "score": round(max(0.0, min(1.0, score)), 4), "touches": len(episodes),
            "touchEpisodes": episodes[-6:], "lastTestAt": candles[last_touch]["timestamp"],
            "lastTouchAgeBars": age, "currentDistanceAtr": round(distance_atr, 4),
            "role": _public_role(state), "state": state, "hardPass": hard_pass,
            "rejectReasons": [] if hard_pass else _reject_reasons(state, len(episodes), age, distance_atr, config),
            "roleFlips": int(state.startswith("role_flip")), "vpConfluence": vp,
            "roundNumber": _is_round_number(center), "memberPivotIds": [item["id"] for item in cluster],
        })
    return sorted(levels, key=lambda item: (-int(item["hardPass"]), -item["score"], item["currentDistanceAtr"], item["id"]))


def _bounded_clusters(pivots, atr_values):
    unassigned = set(range(len(pivots)))
    ordered = sorted(range(len(pivots)), key=lambda index: (-float(pivots[index].get("strength") or 0), -pivots[index]["barIndex"], pivots[index]["id"]))
    clusters = []
    for seed_index in ordered:
        if seed_index not in unassigned: continue
        seed = pivots[seed_index]
        seed_atr = atr_values[seed["barIndex"]]
        members = [seed_index]
        for index in ordered:
            if index == seed_index or index not in unassigned: continue
            pivot = pivots[index]
            candidate_atr = atr_values[pivot["barIndex"]]
            if seed_atr > 0 and candidate_atr > 0 and abs(float(pivot["price"]) - float(seed["price"])) <= 0.60 * statistics.median([seed_atr, candidate_atr]):
                members.append(index)
        while len(members) > 1:
            prices = [float(pivots[index]["price"]) for index in members]
            median_atr = statistics.median([atr_values[pivots[index]["barIndex"]] for index in members])
            if max(prices) - min(prices) <= 0.80 * median_atr: break
            members.remove(max(members, key=lambda index: (abs(float(pivots[index]["price"]) - float(seed["price"])), -float(pivots[index].get("strength") or 0), -pivots[index]["barIndex"])))
        for index in members: unassigned.discard(index)
        clusters.append([pivots[index] for index in members])
    return clusters


def _touch_episodes(candles, low, high, atr_values, config):
    episodes, active, last_end = [], None, -10_000
    for index, row in enumerate(candles):
        overlaps = float(row["low"]) <= high and float(row["high"]) >= low
        if active is None and overlaps and index - last_end >= config.min_touch_gap:
            prior_close = float(candles[index - 1]["close"]) if index else float(row["open"])
            active = {"startIndex": index, "approach": "above" if prior_close > high else "below" if prior_close < low else "inside"}
        if active is None: continue
        local_atr = max(atr_values[index], 1e-12)
        close = float(row["close"])
        reaction = (active["approach"] == "above" and close >= high + 0.75 * local_atr) or (active["approach"] == "below" and close <= low - 0.75 * local_atr)
        failed = (active["approach"] == "above" and close < low - 0.35 * local_atr) or (active["approach"] == "below" and close > high + 0.35 * local_atr)
        if reaction or failed or index - active["startIndex"] >= config.reaction_horizon:
            end = index
            future = candles[end + 1:min(len(candles), end + 1 + config.reaction_horizon)]
            if active["approach"] == "above":
                mfe = max([float(item["high"]) - high for item in future] or [0]) / local_atr
                mae = max([low - float(item["low"]) for item in future] or [0]) / local_atr
            else:
                mfe = max([low - float(item["low"]) for item in future] or [0]) / local_atr
                mae = max([float(item["high"]) - high for item in future] or [0]) / local_atr
            episodes.append({**active, "endIndex": end, "candleKey": candles[active["startIndex"]].get("candleKey"), "outcome": "reaction" if reaction else "failed" if failed else "unresolved", "mfeAtr": round(mfe, 4), "maeAtr": round(mae, 4)})
            active, last_end = None, end
    return episodes


def _role_state(candles, low, high, atr_values, episodes, config):
    reactions = [item for item in episodes if item["outcome"] == "reaction" and item["mfeAtr"] >= 1]
    if not reactions: return "unresolved"
    state = "support_active" if reactions[-1]["approach"] == "above" else "resistance_active"
    start = reactions[-1]["endIndex"] + 1
    for index in range(start, len(candles)):
        close, local_atr = float(candles[index]["close"]), max(atr_values[index], 1e-12)
        if state in {"support_active", "role_flip_support"} and close < low - 0.25 * local_atr: state = "break_down_pending"
        elif state in {"resistance_active", "role_flip_resistance"} and close > high + 0.25 * local_atr: state = "break_up_pending"
        elif state == "break_up_pending" and low <= float(candles[index]["low"]) <= high and close > high: state = "role_flip_support"
        elif state == "break_down_pending" and low <= float(candles[index]["high"]) <= high and close < low: state = "role_flip_resistance"
    return state


def _public_role(state):
    return "support" if state in {"support_active", "role_flip_support"} else "resistance" if state in {"resistance_active", "role_flip_resistance"} else "unresolved"


def _reject_reasons(state, count, age, distance, config):
    reasons = []
    if count < 2: reasons.append("insufficient_touch_episodes")
    if age > config.level_last_touch_max_age: reasons.append("stale")
    if distance > 4: reasons.append("current_distance")
    if state in {"unresolved", "invalidated"}: reasons.append("unresolved_role")
    if state.startswith("break_"): reasons.append("break_pending")
    return reasons


def _weighted_median(values):
    ordered = sorted(values)
    half, cumulative = sum(weight for _, weight in ordered) / 2, 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= half: return value
    return ordered[-1][0]


def _zone_distance(price, low, high): return low - price if price < low else price - high if price > high else 0.0


def _volume_profile_prices(payload):
    result = []
    for item in [payload.get("poc") or {}, payload.get("valueArea") or {}]:
        for key in ("priceMid", "low", "high"):
            if item.get(key) is not None: result.append(float(item[key]))
    return result


def _is_round_number(price):
    if price <= 0: return False
    exponent = 10 ** math.floor(math.log10(price))
    return any(abs(price - factor * exponent) / price <= 0.002 for factor in (1, 2, 5, 10))
