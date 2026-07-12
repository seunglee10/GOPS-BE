from __future__ import annotations

import hashlib
import math
import statistics
from typing import Any, Iterable

from .config import QUALITY_CONFIG


TRIANGLE_KINDS = {"ascending_triangle", "descending_triangle", "symmetrical_triangle"}
FLAG_KINDS = {"bullish_flag", "bearish_flag"}
TRIANGLE_SEARCH_SPANS = (20, 40, 60, 90, 120)
TRIANGLE_MAX_PIVOTS_PER_SIDE = 6
TRIANGLE_RETAINED_PER_STATE = 8
TRIANGLE_MIN_CONVERGENCE = 0.15
TRIANGLE_MAX_CONVERGENCE = 0.85
TRIANGLE_MAX_RESIDUAL_ATR = 0.40
TRIANGLE_MIN_CONTAINMENT = 0.82


def compute_patterns(
    candles: list[dict[str, Any]],
    pivots: list[dict[str, Any]],
    *,
    atr: float,
    interval: str,
) -> list[dict[str, Any]]:
    """Return deterministic, geometry-complete chart-pattern candidates.

    Coordinates are always projected onto timestamps that already exist in the
    canonical candle input. The function emits at most one hard-pass pattern;
    rejected candidates are kept only when they explain an invalid breakout.
    """
    if len(candles) < 20 or atr <= 0 or interval not in QUALITY_CONFIG:
        return []
    candidates = [
        *_triangle_candidates(candles, pivots, atr=atr, interval=interval),
        *_flag_candidates(candles, pivots, atr=atr, interval=interval),
    ]
    passed = sorted(
        (item for item in candidates if item["hardPass"]),
        key=lambda item: (-float(item["score"]), item["id"]),
    )
    rejected = sorted(
        (item for item in candidates if not item["hardPass"]),
        key=lambda item: (-float(item["score"]), item["id"]),
    )
    return [*passed[:1], *rejected[:3]]


def _triangle_candidates(candles, pivots, *, atr, interval):
    candidates = [
        candidate
        for highs, lows, search_span in _triangle_pivot_groups(pivots, len(candles))
        if (candidate := _triangle_candidate(
            candles,
            highs,
            lows,
            atr=atr,
            interval=interval,
            search_span=search_span,
        )) is not None
    ]
    return _retain_triangle_candidates(candidates)


def _triangle_pivot_groups(pivots, candle_count):
    end_index = candle_count - 1
    limit_from = max(0, end_index - max(TRIANGLE_SEARCH_SPANS))
    usable = sorted(
        (
            item for item in pivots
            if item.get("kind") in {"H", "L"}
            and limit_from <= int(item.get("barIndex", -1)) < candle_count
        ),
        key=lambda item: (int(item["barIndex"]), str(item.get("kind")), str(item.get("id"))),
    )
    groups = {}
    for search_span in TRIANGLE_SEARCH_SPANS:
        window_from = max(limit_from, end_index - search_span)
        window = [item for item in usable if int(item["barIndex"]) >= window_from]
        highs = [item for item in window if item["kind"] == "H"][-TRIANGLE_MAX_PIVOTS_PER_SIDE:]
        lows = [item for item in window if item["kind"] == "L"][-TRIANGLE_MAX_PIVOTS_PER_SIDE:]
        for high_subset in _recent_contiguous_subsets(highs):
            for low_subset in _recent_contiguous_subsets(lows):
                if len(high_subset) + len(low_subset) < 5:
                    continue
                start_index = min(int(item["barIndex"]) for item in (*high_subset, *low_subset))
                if end_index - start_index < 20:
                    continue
                key = (
                    tuple(_pivot_identity(item) for item in high_subset),
                    tuple(_pivot_identity(item) for item in low_subset),
                )
                previous = groups.get(key)
                if previous is None or search_span < previous[2]:
                    groups[key] = (high_subset, low_subset, search_span)
    return [groups[key] for key in sorted(groups)]


def _recent_contiguous_subsets(points):
    bounded = list(points)[-TRIANGLE_MAX_PIVOTS_PER_SIDE:]
    if len(bounded) < 2:
        return []
    # A still-forming pattern may have one newly confirmed wick pivot outside
    # its fitted boundary. Evaluate the full recent sequence and, when possible,
    # one variant without that newest pivot. Do not cherry-pick shorter lengths.
    return [bounded, bounded[:-1]] if len(bounded) >= 3 else [bounded]


def _pivot_identity(item):
    return (int(item["barIndex"]), str(item.get("id")), float(item["price"]))


def _triangle_candidate(candles, highs, lows, *, atr, interval, search_span):
    if len(highs) < 2 or len(lows) < 2 or len(highs) + len(lows) < 5:
        return None

    upper_slope, upper_intercept = _fit_points(highs)
    lower_slope, lower_intercept = _fit_points(lows)
    upper_slope_atr = upper_slope / atr
    lower_slope_atr = lower_slope / atr
    kind = _triangle_kind(upper_slope_atr, lower_slope_atr)
    if kind is None:
        return None

    start_index = min(int(item["barIndex"]) for item in (*highs, *lows))
    end_index = len(candles) - 1
    if end_index - start_index < 20:
        return None
    upper_start = _line(upper_slope, upper_intercept, start_index)
    lower_start = _line(lower_slope, lower_intercept, start_index)
    upper_end = _line(upper_slope, upper_intercept, end_index)
    lower_end = _line(lower_slope, lower_intercept, end_index)
    starting_width = upper_start - lower_start
    current_width = upper_end - lower_end
    convergence = current_width / max(starting_width, 1e-12)
    contact_residuals = [
        abs(float(item["price"]) - _line(upper_slope, upper_intercept, int(item["barIndex"]))) / atr
        for item in highs
    ] + [
        abs(float(item["price"]) - _line(lower_slope, lower_intercept, int(item["barIndex"]))) / atr
        for item in lows
    ]

    state, breakout_direction, breakout_index = _breakout_state(
        candles,
        upper_slope=upper_slope,
        upper_intercept=upper_intercept,
        lower_slope=lower_slope,
        lower_intercept=lower_intercept,
        atr=atr,
        expected=_expected_breakout(kind),
    )
    containment_end = breakout_index - 1 if breakout_index is not None else end_index
    containment = _containment(
        candles,
        start_index,
        containment_end,
        upper_slope,
        upper_intercept,
        lower_slope,
        lower_intercept,
        atr,
    )
    evidence_pass = (
        len(highs) >= 2
        and len(lows) >= 2
        and len(highs) + len(lows) >= 5
        and max(contact_residuals, default=99.0) <= TRIANGLE_MAX_RESIDUAL_ATR
        and starting_width >= 2 * atr
        and TRIANGLE_MIN_CONVERGENCE <= convergence <= TRIANGLE_MAX_CONVERGENCE
        and containment >= TRIANGLE_MIN_CONTAINMENT
    )
    active_pass = state in {"forming", "confirmed"}
    hard_pass = evidence_pass and active_pass
    score = (
        0.25 * min(1.0, (len(highs) + len(lows)) / 6)
        + 0.25 * containment
        + 0.20 * (1 - min(1.0, max(contact_residuals, default=1.0) / TRIANGLE_MAX_RESIDUAL_ATR))
        + 0.15 * (1 - abs(convergence - 0.45) / 0.45)
        + 0.15 * (1 if state == "confirmed" else 0.8)
    )
    reasons = []
    if not evidence_pass:
        reasons.append("triangle_geometry")
    if state == "invalidated":
        reasons.append("opposite_breakout")

    geometry = {
        "upper": _boundary(candles, start_index, end_index, upper_slope, upper_intercept),
        "lower": _boundary(candles, start_index, end_index, lower_slope, lower_intercept),
    }
    evidence_refs = [str(item.get("id")) for item in (*highs, *lows)]
    raw = f"{interval}|{kind}|{start_index}|{end_index}|{evidence_refs}|{geometry['upper']}|{geometry['lower']}"
    return {
        "id": f"{interval}:pattern:{hashlib.sha256(raw.encode()).hexdigest()[:10]}",
        "kind": kind,
        "state": state,
        "breakoutDirection": breakout_direction,
        "hardPass": hard_pass,
        "evidencePass": evidence_pass,
        "activePass": active_pass,
        "score": round(max(0.0, min(1.0, score)), 4),
        "touches": len(highs) + len(lows),
        "upperTouches": len(highs),
        "lowerTouches": len(lows),
        "containment": round(containment, 4),
        "convergenceRatio": round(convergence, 4),
        "maxResidualAtr": round(max(contact_residuals, default=0.0), 4),
        "spanBars": end_index - start_index,
        "searchWindowBars": search_span,
        "geometry": geometry,
        "evidenceRefs": evidence_refs,
        "rejectReasons": reasons,
    }


def _retain_triangle_candidates(candidates):
    ranked = lambda items: sorted(items, key=lambda item: (-float(item["score"]), item["id"]))
    passed = ranked(item for item in candidates if item["hardPass"])
    rejected = ranked(item for item in candidates if not item["hardPass"])
    return [
        *passed[:TRIANGLE_RETAINED_PER_STATE],
        *rejected[:TRIANGLE_RETAINED_PER_STATE],
    ]


def _flag_candidates(candles, pivots, *, atr, interval):
    ordered = sorted(pivots, key=lambda item: int(item.get("barIndex", -1)))
    results = []
    for first, second in zip(ordered, ordered[1:]):
        start = int(first.get("barIndex", -1))
        pole_end = int(second.get("barIndex", -1))
        pole_bars = pole_end - start
        if not 3 <= pole_bars <= 20:
            continue
        if (first.get("kind"), second.get("kind")) == ("L", "H"):
            kind, direction = "bullish_flag", 1
        elif (first.get("kind"), second.get("kind")) == ("H", "L"):
            kind, direction = "bearish_flag", -1
        else:
            continue
        pole_move = direction * (float(second["price"]) - float(first["price"]))
        if pole_move < 4 * atr:
            continue
        path = sum(
            abs(float(right["close"]) - float(left["close"]))
            for left, right in zip(candles[start:pole_end + 1], candles[start + 1:pole_end + 1])
        )
        efficiency = pole_move / max(path, pole_move, 1e-12)
        if efficiency < 0.70:
            continue

        for fit_end in dict.fromkeys((len(candles) - 1, len(candles) - 2)):
            flag_start = pole_end + 1
            flag_bars = fit_end - flag_start + 1
            if not 5 <= flag_bars <= 30:
                continue
            flag_rows = candles[flag_start:fit_end + 1]
            upper_points = [(flag_start + index, float(row["high"])) for index, row in enumerate(flag_rows)]
            lower_points = [(flag_start + index, float(row["low"])) for index, row in enumerate(flag_rows)]
            upper_slope, upper_intercept = _fit_xy(upper_points)
            lower_slope, lower_intercept = _fit_xy(lower_points)
            slope_error = abs(upper_slope - lower_slope) / atr
            channel_slope = (upper_slope + lower_slope) / (2 * atr)
            counter_trend = channel_slope <= 0.02 if direction > 0 else channel_slope >= -0.02
            upper_contacts = _contact_count(upper_points, upper_slope, upper_intercept, atr, interval)
            lower_contacts = _contact_count(lower_points, lower_slope, lower_intercept, atr, interval)
            containment = _containment(
                candles,
                flag_start,
                fit_end,
                upper_slope,
                upper_intercept,
                lower_slope,
                lower_intercept,
                atr,
            )
            if direction > 0:
                retracement = (float(second["price"]) - min(float(row["low"]) for row in flag_rows)) / pole_move
            else:
                retracement = (max(float(row["high"]) for row in flag_rows) - float(second["price"])) / pole_move
            state, breakout_direction, breakout_index = _breakout_state(
                candles,
                upper_slope=upper_slope,
                upper_intercept=upper_intercept,
                lower_slope=lower_slope,
                lower_intercept=lower_intercept,
                atr=atr,
                expected="up" if direction > 0 else "down",
                inspect_from=fit_end + 1,
            )
            evidence_pass = (
                0.10 <= retracement <= 0.50
                and slope_error <= 0.05
                and counter_trend
                and upper_contacts >= 2
                and lower_contacts >= 2
                and containment >= 0.80
            )
            active_pass = state in {"forming", "confirmed"}
            hard_pass = evidence_pass and active_pass
            score = (
                0.25 * min(1.0, pole_move / (8 * atr))
                + 0.20 * efficiency
                + 0.20 * containment
                + 0.15 * (1 - min(1.0, slope_error / 0.05))
                + 0.10 * (1 - min(1.0, abs(retracement - 0.30) / 0.20))
                + 0.10 * (1 if state == "confirmed" else 0.8)
            )
            geometry = {
                "pole": {
                    "start": _point(candles[start], float(first["price"])),
                    "end": _point(candles[pole_end], float(second["price"])),
                },
                "upper": _boundary(candles, flag_start, fit_end, upper_slope, upper_intercept),
                "lower": _boundary(candles, flag_start, fit_end, lower_slope, lower_intercept),
            }
            reasons = []
            if not evidence_pass:
                reasons.append("flag_geometry")
            if state == "invalidated":
                reasons.append("opposite_breakout")
            raw = f"{interval}|{kind}|{start}|{pole_end}|{fit_end}|{geometry}"
            results.append({
                "id": f"{interval}:pattern:{hashlib.sha256(raw.encode()).hexdigest()[:10]}",
                "kind": kind,
                "state": state,
                "breakoutDirection": breakout_direction,
                "hardPass": hard_pass,
                "evidencePass": evidence_pass,
                "activePass": active_pass,
                "score": round(max(0.0, min(1.0, score)), 4),
                "touches": upper_contacts + lower_contacts,
                "upperTouches": upper_contacts,
                "lowerTouches": lower_contacts,
                "containment": round(containment, 4),
                "poleAtr": round(pole_move / atr, 4),
                "poleEfficiency": round(efficiency, 4),
                "retracementRatio": round(retracement, 4),
                "parallelSlopeErrorAtr": round(slope_error, 4),
                "spanBars": pole_end - start + flag_bars,
                "geometry": geometry,
                "evidenceRefs": [str(first.get("id")), str(second.get("id"))],
                "rejectReasons": reasons,
            })
    return results


def _triangle_kind(upper_slope_atr: float, lower_slope_atr: float) -> str | None:
    flat = 0.05
    directional = 0.01
    if upper_slope_atr <= -directional and lower_slope_atr >= directional:
        return "symmetrical_triangle"
    if abs(upper_slope_atr) <= flat and lower_slope_atr >= directional:
        return "ascending_triangle"
    if abs(lower_slope_atr) <= flat and upper_slope_atr <= -directional:
        return "descending_triangle"
    return None


def _expected_breakout(kind: str) -> str:
    if kind == "ascending_triangle":
        return "up"
    if kind == "descending_triangle":
        return "down"
    return "either"


def _breakout_state(
    candles,
    *,
    upper_slope,
    upper_intercept,
    lower_slope,
    lower_intercept,
    atr,
    expected,
    inspect_from=None,
):
    start = max(0, int(inspect_from)) if inspect_from is not None else max(0, len(candles) - 2)
    baseline_volumes = [float(row.get("volume") or 0) for row in candles[max(0, len(candles) - 21):-1]]
    median_volume = statistics.median(baseline_volumes) if baseline_volumes else 0.0
    detected = None
    for index in range(start, len(candles)):
        close = float(candles[index]["close"])
        upper = _line(upper_slope, upper_intercept, index)
        lower = _line(lower_slope, lower_intercept, index)
        direction = "up" if close > upper + 0.25 * atr else "down" if close < lower - 0.25 * atr else None
        if direction is not None:
            detected = (index, direction)
            break
    if detected is None:
        return "forming", None, None
    index, direction = detected
    if expected not in {"either", direction}:
        return "invalidated", direction, index
    held = index + 1 < len(candles) and (
        float(candles[index + 1]["close"]) > _line(upper_slope, upper_intercept, index + 1) + 0.25 * atr
        if direction == "up"
        else float(candles[index + 1]["close"]) < _line(lower_slope, lower_intercept, index + 1) - 0.25 * atr
    )
    volume_confirmed = median_volume > 0 and float(candles[index].get("volume") or 0) >= 1.5 * median_volume
    if held or volume_confirmed:
        return "confirmed", direction, index
    return "forming", None, None


def _containment(candles, start, end, upper_slope, upper_intercept, lower_slope, lower_intercept, atr):
    if end < start:
        return 0.0
    contained = 0
    count = 0
    for index in range(start, end + 1):
        upper = _line(upper_slope, upper_intercept, index)
        lower = _line(lower_slope, lower_intercept, index)
        close = float(candles[index]["close"])
        contained += lower - 0.35 * atr <= close <= upper + 0.35 * atr
        count += 1
    return contained / max(1, count)


def _fit_points(points: list[dict[str, Any]]) -> tuple[float, float]:
    return _fit_xy([(int(item["barIndex"]), float(item["price"])) for item in points])


def _fit_xy(points: Iterable[tuple[int, float]]) -> tuple[float, float]:
    values = list(points)
    xs = [float(item[0]) for item in values]
    ys = [float(item[1]) for item in values]
    x_mean, y_mean = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator if denominator else 0.0
    return slope, y_mean - slope * x_mean


def _line(slope: float, intercept: float, index: int) -> float:
    return intercept + slope * index


def _point(row: dict[str, Any], price: float) -> dict[str, Any]:
    return {"timestamp": row["timestamp"], "price": round(price, 6)}


def _boundary(candles, start, end, slope, intercept):
    return {
        "start": _point(candles[start], _line(slope, intercept, start)),
        "end": _point(candles[end], _line(slope, intercept, end)),
    }


def _contact_count(points, slope, intercept, atr, interval):
    tolerance = 0.35 * atr
    gap = max(1, QUALITY_CONFIG[interval].min_touch_gap)
    contacts = [index for index, price in points if abs(price - _line(slope, intercept, index)) <= tolerance]
    episodes = []
    for index in contacts:
        if not episodes or index - episodes[-1] >= gap:
            episodes.append(index)
    return len(episodes)
