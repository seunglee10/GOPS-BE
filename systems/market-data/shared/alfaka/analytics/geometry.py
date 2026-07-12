from __future__ import annotations

import hashlib
import itertools
import statistics
from typing import Any, Iterable

from .atr import latest_atr
from .patterns import compute_triangles
from .pivots import compute_pivots


SUPPORTED_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")
TARGET_BARS = {**{interval: 380 for interval in SUPPORTED_INTERVALS[:-1]}, "1W": 312}
WARMUP_BARS = {interval: 120 for interval in SUPPORTED_INTERVALS}
EVALUATION_BARS = {**{interval: 260 for interval in SUPPORTED_INTERVALS[:-1]}, "1W": 192}
MINIMUM_BARS = 120
ALGORITHM_VERSION = "ohlcv-consensus-regression-triangles"

_ATR_PERIOD = 14
_VOLUME_BASELINE = 20
_TOUCH_TOLERANCE_ATR = 0.35
_MIN_TOUCH_GAP = 5
_TRIANGLE_SPANS = (20, 40, 60, 90, 120)
_TRIANGLE_KINDS = ("ascending_triangle", "descending_triangle", "symmetrical_triangle")


def analyze_geometry(symbol: str, interval: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported geometry interval: {interval}")
    rows = [dict(row) for row in candles if row.get("isClosed", row.get("is_closed", True)) is not False]
    if len(rows) < MINIMUM_BARS:
        raise ValueError(f"Geometry analysis requires at least {MINIMUM_BARS} completed candles")
    rows = rows[-TARGET_BARS[interval]:]
    for index, row in enumerate(rows):
        row["barIndex"] = index
    atr_values = _wilder_atr(rows)
    evaluation_from = max(0, len(rows) - EVALUATION_BARS[interval])
    evidence = _pivot_evidence(rows, atr_values, evaluation_from=evaluation_from)
    latest_atr = max(atr_values[-1], 1e-12)
    current = float(rows[-1]["close"])
    supports = _horizontal_levels(
        symbol, interval, rows, [item for item in evidence if item["kind"] == "L"],
        role="support", current=current, atr=latest_atr,
    )
    resistances = _horizontal_levels(
        symbol, interval, rows, [item for item in evidence if item["kind"] == "H"],
        role="resistance", current=current, atr=latest_atr,
    )
    triangle_candidates = _regression_triangle_candidates(rows, interval=interval)
    active = sorted(
        (item for item in triangle_candidates if item["hardPass"] and item["state"] in {"forming", "confirmed"}),
        key=lambda item: (-item["score"], -item["endIndex"], item["geometryHash"]),
    )
    primary = active[0] if active else None
    historical = next(
        (item for item in sorted(triangle_candidates, key=lambda item: (-item["score"], -item["endIndex"], item["geometryHash"])) if not primary or item["geometryHash"] != primary["geometryHash"]),
        None,
    )
    generated_at = str(rows[-1]["timestamp"])
    drawings = [
        *[_level_drawing(symbol, interval, item, generated_at) for item in (*supports, *resistances)],
        *(_triangle_drawings(symbol, interval, primary, generated_at) if primary else []),
    ][:6]
    return {
        "algorithmVersion": ALGORITHM_VERSION,
        "supports": supports,
        "resistances": resistances,
        "primaryTriangle": _public_triangle(primary),
        "historicalTriangle": _public_triangle(historical),
        "indicators": compute_sma_snapshot(rows),
        "drawings": drawings,
        "evidence": [dict(item) for item in sorted(evidence, key=lambda value: (-value["score"], -value["barIndex"]))[:24]],
    }


def compute_sma_snapshot(candles: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(row["close"]) for row in candles]
    if len(closes) < MINIMUM_BARS:
        return {"sma60": None, "sma120": None, "cross": {"status": "data_insufficient"}}
    prefix = [0.0]
    for value in closes:
        prefix.append(prefix[-1] + value)

    def average(end: int, period: int) -> float:
        return (prefix[end + 1] - prefix[end + 1 - period]) / period

    latest = len(closes) - 1
    sma60 = average(latest, 60)
    sma120 = average(latest, 120)
    if len(closes) == MINIMUM_BARS:
        cross = {"status": "insufficient_previous_bar", "direction": None, "timestamp": None, "barsAgo": None}
    else:
        events: list[dict[str, Any]] = []
        for index in range(120, len(closes)):
            previous_short, current_short = average(index - 1, 60), average(index, 60)
            previous_long, current_long = average(index - 1, 120), average(index, 120)
            direction = (
                "golden" if previous_short <= previous_long and current_short > current_long
                else "dead" if previous_short >= previous_long and current_short < current_long
                else None
            )
            if direction:
                events.append({
                    "status": "crossed", "direction": direction,
                    "timestamp": candles[index]["timestamp"], "barsAgo": len(closes) - 1 - index,
                    "shortPeriod": 60, "longPeriod": 120,
                })
        cross = events[-1] if events else {"status": "none", "direction": None, "timestamp": None, "barsAgo": None}
    return {"sma60": round(sma60, 6), "sma120": round(sma120, 6), "cross": cross}


def _pivot_evidence(rows: list[dict[str, Any]], atr_values: list[float], *, evaluation_from: int) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index in range(max(2, evaluation_from), len(rows) - 3):
        row = rows[index]
        high, low, close = (float(row[key]) for key in ("high", "low", "close"))
        local_high = high >= max(float(item["high"]) for item in rows[index - 2:index + 3])
        local_low = low <= min(float(item["low"]) for item in rows[index - 2:index + 3])
        if not local_high and not local_low:
            continue
        atr = max(atr_values[index], 1e-12)
        candle_range = max(high - low, 1e-12)
        baseline = [float(item.get("volume") or 0) for item in rows[max(0, index - _VOLUME_BASELINE):index]]
        median_volume = statistics.median(baseline) if baseline else 0.0
        relative_volume = float(row.get("volume") or 0) / median_volume if median_volume > 0 else 0.0
        future = rows[index + 1:index + 4]
        for kind in (("H",) if local_high and not local_low else ("L",) if local_low and not local_high else ("H", "L")):
            if kind == "H":
                price = high
                reversal = price - min(float(item["low"]) for item in future)
                wick = high - max(float(row["open"]), close)
                rejection = (high - close) / candle_range
                prominence = high - max(float(rows[index - 1]["high"]), float(rows[index + 1]["high"]))
            else:
                price = low
                reversal = max(float(item["high"]) for item in future) - price
                wick = min(float(row["open"]), close) - low
                rejection = (close - low) / candle_range
                prominence = min(float(rows[index - 1]["low"]), float(rows[index + 1]["low"])) - low
            components = {
                "reversal": _cap(reversal / (2 * atr)),
                "wick": _cap(wick / candle_range),
                "closeRejection": _cap(rejection),
                "relativeVolume": _cap(relative_volume / 3),
                "prominence": _cap(prominence / (2 * atr)),
                "rangeExpansion": _cap(candle_range / (2 * atr)),
            }
            score = (
                0.30 * components["reversal"] + 0.20 * components["wick"]
                + 0.15 * components["closeRejection"] + 0.15 * components["relativeVolume"]
                + 0.10 * components["prominence"] + 0.10 * components["rangeExpansion"]
            )
            if reversal < 0.75 * atr or score < 0.55:
                continue
            identity = f"{kind}|{row['timestamp']}|{price:.8f}"
            evidence.append({
                "id": f"pivot-{hashlib.sha256(identity.encode()).hexdigest()[:12]}",
                "kind": kind, "barIndex": index, "timestamp": row["timestamp"], "price": round(price, 6),
                "atr": round(atr, 6), "score": round(score, 4), "relativeVolume": round(relative_volume, 4),
                "features": {key: round(value, 4) for key, value in components.items()},
            })
    return evidence


def _horizontal_levels(symbol, interval, rows, candidates, *, role, current, atr):
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda item: (item["price"], item["barIndex"], item["id"])):
        matching = next((group for group in groups if abs(candidate["price"] - statistics.median(item["price"] for item in group)) <= _TOUCH_TOLERANCE_ATR * atr), None)
        if matching is None:
            groups.append([candidate])
        else:
            matching.append(candidate)
    levels = []
    for group in groups:
        contacts = _independent_contacts(group)
        if len(contacts) < 2:
            continue
        price = _weighted_median([(item["price"], item["score"]) for item in contacts])
        age = len(rows) - 1 - contacts[-1]["barIndex"]
        distance = abs(current - price) / atr
        score = (
            0.45 * statistics.mean(item["score"] for item in contacts)
            + 0.20 * min(1.0, len(contacts) / 4)
            + 0.20 * (1 - min(1.0, age / max(1, EVALUATION_BARS[interval])))
            + 0.15 * (1 - min(1.0, distance / 4))
        )
        identity = f"{symbol}|{interval}|{role}|{price:.8f}|{'|'.join(item['id'] for item in contacts)}"
        levels.append({
            "id": f"{role}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}", "role": role,
            "price": round(price, 6), "score": round(score, 4), "touches": len(contacts),
            "lastTouchAgeBars": age, "currentDistanceAtr": round(distance, 4),
            "anchors": [
                {"timestamp": contacts[0]["timestamp"], "price": round(price, 6)},
                {"timestamp": contacts[-1]["timestamp"], "price": round(price, 6)},
            ],
            "evidence": contacts[:8],
        })
    return sorted(levels, key=lambda item: (-item["score"], item["currentDistanceAtr"], item["id"]))[:2]


def _regression_triangle_candidates(rows, *, interval):
    display_from = rows[max(0, len(rows) - EVALUATION_BARS[interval])]["timestamp"]
    pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    candidates = compute_triangles(rows, pivots, atr=latest_atr(rows), interval=interval)
    evidence_by_id = {str(item["id"]): item for item in pivots}
    end = len(rows) - 1
    results = []
    for candidate in candidates:
        geometry = candidate["geometry"]
        start = max(0, end - int(candidate["spanBars"]))
        span = max(1, end - start)
        upper_slope = (float(geometry["upper"]["end"]["price"]) - float(geometry["upper"]["start"]["price"])) / span
        lower_slope = (float(geometry["lower"]["end"]["price"]) - float(geometry["lower"]["start"]["price"])) / span
        upper_intercept = float(geometry["upper"]["start"]["price"]) - upper_slope * start
        lower_intercept = float(geometry["lower"]["start"]["price"]) - lower_slope * start
        results.append({
            **candidate,
            "startIndex": start,
            "endIndex": end,
            "geometryHash": hashlib.sha256(str(candidate["id"]).encode()).hexdigest()[:16],
            "upper": geometry["upper"],
            "lower": geometry["lower"],
            "apexBarsFromAsOf": _apex_bars(end, upper_slope, upper_intercept, lower_slope, lower_intercept),
            "evidence": [evidence_by_id[ref] for ref in candidate.get("evidenceRefs", []) if ref in evidence_by_id],
        })
    return results


def _triangle_candidates(rows, evidence, *, atr, interval):
    results: dict[str, dict[str, Any]] = {}
    end = len(rows) - 1
    for span in _TRIANGLE_SPANS:
        start_limit = max(0, end - span + 1)
        highs = _bounded_side([item for item in evidence if item["kind"] == "H" and item["barIndex"] >= start_limit])
        lows = _bounded_side([item for item in evidence if item["kind"] == "L" and item["barIndex"] >= start_limit])
        if len(highs) < 2 or len(lows) < 2:
            continue
        for high_pair, low_pair in itertools.product(itertools.combinations(highs, 2), itertools.combinations(lows, 2)):
            candidate = _triangle_candidate(rows, highs, lows, high_pair, low_pair, atr=atr, interval=interval, span=span)
            if candidate is not None:
                current = results.get(candidate["geometryHash"])
                if current is None or candidate["score"] > current["score"]:
                    results[candidate["geometryHash"]] = candidate
    return list(results.values())


def _triangle_candidate(rows, highs, lows, high_pair, low_pair, *, atr, interval, span):
    upper_slope, upper_intercept = _line_from_pair(high_pair)
    lower_slope, lower_intercept = _line_from_pair(low_pair)
    kind = _triangle_kind(upper_slope / atr, lower_slope / atr)
    if not kind:
        return None
    upper = _independent_contacts([item for item in highs if abs(item["price"] - _line(upper_slope, upper_intercept, item["barIndex"])) <= _TOUCH_TOLERANCE_ATR * atr])
    lower = _independent_contacts([item for item in lows if abs(item["price"] - _line(lower_slope, lower_intercept, item["barIndex"])) <= _TOUCH_TOLERANCE_ATR * atr])
    if len(upper) < 2 or len(lower) < 2 or len(upper) + len(lower) < 5:
        return None
    start = min(item["barIndex"] for item in (*upper, *lower))
    end = len(rows) - 1
    upper_start, lower_start = _line(upper_slope, upper_intercept, start), _line(lower_slope, lower_intercept, start)
    upper_end, lower_end = _line(upper_slope, upper_intercept, end), _line(lower_slope, lower_intercept, end)
    starting_width = upper_start - lower_start
    current_width = upper_end - lower_end
    if starting_width < 2 * atr or current_width <= 0:
        return None
    convergence = current_width / starting_width
    if not 0.15 <= convergence <= 0.85:
        return None
    residuals = [abs(item["price"] - _line(upper_slope, upper_intercept, item["barIndex"])) / atr for item in upper]
    residuals += [abs(item["price"] - _line(lower_slope, lower_intercept, item["barIndex"])) / atr for item in lower]
    max_residual = max(residuals)
    if max_residual > _TOUCH_TOLERANCE_ATR:
        return None
    contained = sum(
        _line(lower_slope, lower_intercept, index) - _TOUCH_TOLERANCE_ATR * atr <= float(rows[index]["close"]) <= _line(upper_slope, upper_intercept, index) + _TOUCH_TOLERANCE_ATR * atr
        for index in range(start, end + 1)
    )
    containment = contained / max(1, end - start + 1)
    if containment < 0.82:
        return None
    state, breakout_direction = _triangle_state(rows, kind, upper_slope, upper_intercept, lower_slope, lower_intercept, atr)
    touch_quality = statistics.mean(item["score"] for item in (*upper, *lower))
    residual_quality = 1 - min(1.0, max_residual / _TOUCH_TOLERANCE_ATR)
    convergence_quality = 1 - min(1.0, abs(convergence - 0.45) / 0.45)
    recency = 1 - min(1.0, (end - max(item["barIndex"] for item in (*upper, *lower))) / max(1, span))
    score = 0.30 * touch_quality + 0.25 * containment + 0.20 * residual_quality + 0.15 * convergence_quality + 0.10 * recency
    geometry_hash = hashlib.sha256(
        f"{interval}|{kind}|{start}|{end}|{upper_slope:.10f}|{lower_slope:.10f}|{'|'.join(item['id'] for item in (*upper, *lower))}".encode()
    ).hexdigest()[:16]
    return {
        "kind": kind, "state": state, "breakoutDirection": breakout_direction,
        "score": round(score, 4), "touches": len(upper) + len(lower),
        "upperTouches": len(upper), "lowerTouches": len(lower), "containment": round(containment, 4),
        "convergenceRatio": round(convergence, 4), "maxResidualAtr": round(max_residual, 4),
        "startIndex": start, "endIndex": end, "geometryHash": geometry_hash,
        "upper": _boundary(rows, start, end, upper_slope, upper_intercept),
        "lower": _boundary(rows, start, end, lower_slope, lower_intercept),
        "apexBarsFromAsOf": _apex_bars(end, upper_slope, upper_intercept, lower_slope, lower_intercept),
        "evidence": [*upper, *lower],
    }


def _triangle_state(rows, kind, upper_slope, upper_intercept, lower_slope, lower_intercept, atr):
    expected = "up" if kind == "ascending_triangle" else "down" if kind == "descending_triangle" else "either"
    detected = None
    for index in range(max(0, len(rows) - 5), len(rows)):
        close = float(rows[index]["close"])
        direction = "up" if close > _line(upper_slope, upper_intercept, index) + 0.25 * atr else "down" if close < _line(lower_slope, lower_intercept, index) - 0.25 * atr else None
        if direction:
            detected = (index, direction)
            break
    if detected:
        index, direction = detected
        if expected not in {"either", direction}:
            return "invalidated", direction
        held = index + 1 < len(rows) and (
            float(rows[index + 1]["close"]) > _line(upper_slope, upper_intercept, index + 1) + 0.25 * atr if direction == "up"
            else float(rows[index + 1]["close"]) < _line(lower_slope, lower_intercept, index + 1) - 0.25 * atr
        )
        baseline = [float(item.get("volume") or 0) for item in rows[max(0, index - 20):index]]
        volume_confirmed = bool(baseline) and float(rows[index].get("volume") or 0) >= 1.5 * statistics.median(baseline)
        return ("confirmed", direction) if held or volume_confirmed else ("inactive", direction)
    latest = len(rows) - 1
    close = float(rows[-1]["close"])
    inside = _line(lower_slope, lower_intercept, latest) <= close <= _line(upper_slope, upper_intercept, latest)
    return ("forming", None) if inside else ("inactive", None)


def _level_drawing(symbol, interval, level, generated_at):
    color = "#22c55e" if level["role"] == "support" else "#ef4444"
    return _drawing(symbol, interval, level["id"], "horizontalLine", level["anchors"], color, "지지" if level["role"] == "support" else "저항", generated_at, opacity=0.86)


def _triangle_drawings(symbol, interval, triangle, generated_at):
    color = "#22c55e" if triangle["kind"] == "ascending_triangle" else "#ef4444" if triangle["kind"] == "descending_triangle" else "#f59e0b"
    name = {"ascending_triangle": "상승 삼각형", "descending_triangle": "하락 삼각형", "symmetrical_triangle": "대칭 삼각형"}[triangle["kind"]]
    state = "돌파 확인" if triangle["state"] == "confirmed" else "형성 중"
    opacity = 0.95 if triangle["state"] == "confirmed" else 0.58
    return [
        _drawing(symbol, interval, f"{triangle['geometryHash']}-upper", "trendLine", [triangle["upper"]["start"], triangle["upper"]["end"]], color, f"{name} · {state}", generated_at, opacity=opacity),
        _drawing(symbol, interval, f"{triangle['geometryHash']}-lower", "trendLine", [triangle["lower"]["start"], triangle["lower"]["end"]], color, f"{name} · {state}", generated_at, opacity=opacity),
    ]


def _drawing(symbol, interval, suffix, drawing_type, anchors, color, label, generated_at, *, opacity):
    return {
        "id": f"chart-asset:{symbol.upper()}:{interval}:{suffix}", "type": drawing_type,
        "symbol": symbol.upper(), "interval": interval, "sourceInterval": interval,
        "anchors": anchors,
        "style": {"color": color, "lineWidth": 2, "lineDash": [], "lineStyle": "solid", "opacity": opacity, "extension": "segment"},
        "label": label, "locked": False, "visible": True, "createdBy": "system",
        "sourceProposalId": f"chart-asset:{symbol.upper()}:{interval}:geometry",
        "createdAt": generated_at, "updatedAt": generated_at,
    }


def _public_triangle(value):
    if value is None:
        return None
    return {key: value[key] for key in (
        "kind", "state", "breakoutDirection", "score", "touches", "upperTouches", "lowerTouches",
        "containment", "convergenceRatio", "maxResidualAtr", "geometryHash", "upper", "lower",
        "apexBarsFromAsOf", "evidence",
    )}


def _wilder_atr(rows):
    true_ranges = []
    for index, row in enumerate(rows):
        high, low = float(row["high"]), float(row["low"])
        previous_close = float(rows[index - 1]["close"]) if index else float(row["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    values = []
    current = true_ranges[0]
    for index, value in enumerate(true_ranges):
        if index < _ATR_PERIOD:
            current = statistics.mean(true_ranges[:index + 1])
        else:
            current = (current * (_ATR_PERIOD - 1) + value) / _ATR_PERIOD
        values.append(current)
    return values


def _independent_contacts(values):
    result = []
    for item in sorted(values, key=lambda value: (value["barIndex"], -value["score"], value["id"])):
        if not result or item["barIndex"] - result[-1]["barIndex"] >= _MIN_TOUCH_GAP:
            result.append(item)
        elif item["score"] > result[-1]["score"]:
            result[-1] = item
    return result


def _bounded_side(values):
    recent = sorted(values, key=lambda item: (item["barIndex"], item["id"]))[-10:]
    return sorted(recent, key=lambda item: (item["barIndex"], item["id"]))


def _line_from_pair(pair):
    first, second = pair
    distance = second["barIndex"] - first["barIndex"]
    if not distance:
        return 0.0, float(first["price"])
    slope = (float(second["price"]) - float(first["price"])) / distance
    return slope, float(first["price"]) - slope * first["barIndex"]


def _triangle_kind(upper_slope_atr, lower_slope_atr):
    if upper_slope_atr <= -0.01 and lower_slope_atr >= 0.01:
        return "symmetrical_triangle"
    if abs(upper_slope_atr) <= 0.03 and lower_slope_atr >= 0.01:
        return "ascending_triangle"
    if abs(lower_slope_atr) <= 0.03 and upper_slope_atr <= -0.01:
        return "descending_triangle"
    return None


def _boundary(rows, start, end, slope, intercept):
    return {
        "start": {"timestamp": rows[start]["timestamp"], "price": round(_line(slope, intercept, start), 6)},
        "end": {"timestamp": rows[end]["timestamp"], "price": round(_line(slope, intercept, end), 6)},
    }


def _apex_bars(end, upper_slope, upper_intercept, lower_slope, lower_intercept):
    difference = upper_slope - lower_slope
    if abs(difference) < 1e-12:
        return None
    apex = (lower_intercept - upper_intercept) / difference
    return round(apex - end, 4)


def _weighted_median(values: Iterable[tuple[float, float]]) -> float:
    ordered = sorted(values)
    total = sum(weight for _value, weight in ordered)
    cursor = 0.0
    for value, weight in ordered:
        cursor += weight
        if cursor >= total / 2:
            return float(value)
    return float(ordered[-1][0])


def _line(slope, intercept, index):
    return intercept + slope * index


def _cap(value):
    return max(0.0, min(1.0, float(value)))
