from __future__ import annotations

import hashlib
import math
import statistics
from typing import Any, Iterable

from .config import QUALITY_CONFIG


TRIANGLE_KINDS = {"ascending_triangle", "descending_triangle", "symmetrical_triangle"}
FLAG_KINDS = {"bullish_flag", "bearish_flag"}
PENNANT_KINDS = {"bullish_pennant", "bearish_pennant"}
RECTANGLE_KINDS = {"bullish_rectangle", "bearish_rectangle"}
WEDGE_KINDS = {"rising_wedge", "falling_wedge"}
CHANNEL_BREAK_KINDS = {"descending_channel_breakout", "ascending_channel_breakdown"}
PATTERN_KINDS = TRIANGLE_KINDS | FLAG_KINDS | PENNANT_KINDS | RECTANGLE_KINDS | WEDGE_KINDS | CHANNEL_BREAK_KINDS
TRIANGLE_SEARCH_SPANS = (20, 40, 60, 90, 120)
TRIANGLE_MAX_PIVOTS_PER_SIDE = 6
TRIANGLE_RETAINED_PER_STATE = 8
TRIANGLE_MIN_CONVERGENCE = 0.15
TRIANGLE_MAX_CONVERGENCE = 0.85
TRIANGLE_MAX_RESIDUAL_ATR = 0.40
TRIANGLE_MIN_CONTAINMENT = 0.82
_PATTERN_PRIORITY = {
    **{kind: 0 for kind in PENNANT_KINDS | FLAG_KINDS | RECTANGLE_KINDS},
    **{kind: 1 for kind in WEDGE_KINDS | CHANNEL_BREAK_KINDS},
    **{kind: 2 for kind in TRIANGLE_KINDS},
}


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
        *compute_triangles(candles, pivots, atr=atr, interval=interval),
        *_flag_candidates(candles, pivots, atr=atr, interval=interval),
        *_continuation_candidates(candles, pivots, atr=atr, interval=interval),
        *_sloped_boundary_candidates(candles, pivots, atr=atr, interval=interval),
    ]
    passed = sorted(
        (item for item in candidates if item["hardPass"]),
        key=lambda item: (_PATTERN_PRIORITY[item["kind"]], -float(item["score"]), item["id"]),
    )
    rejected = sorted(
        (item for item in candidates if not item["hardPass"]),
        key=lambda item: (-float(item["score"]), item["id"]),
    )
    return [*passed[:1], *rejected[:3]]


def compute_triangles(
    candles: list[dict[str, Any]],
    pivots: list[dict[str, Any]],
    *,
    atr: float,
    interval: str,
) -> list[dict[str, Any]]:
    """Return the previous regression-fitted triangle candidates only."""
    if len(candles) < 20 or atr <= 0 or interval not in QUALITY_CONFIG:
        return []
    return _triangle_candidates(candles, pivots, atr=atr, interval=interval)


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

    state, breakout_direction, breakout_index, confirmation = _breakout_state(
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
        "confirmation": confirmation,
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
    results = []
    for impulse in _impulse_pairs(candles, pivots, atr=atr):
        first, second = impulse["first"], impulse["second"]
        start, pole_end = impulse["start"], impulse["poleEnd"]
        direction, pole_move, efficiency = impulse["direction"], impulse["poleMove"], impulse["efficiency"]
        kind = "bullish_flag" if direction > 0 else "bearish_flag"
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
            channel_width_atr = (
                (_line(upper_slope, upper_intercept, flag_start) - _line(lower_slope, lower_intercept, flag_start))
                + (_line(upper_slope, upper_intercept, fit_end) - _line(lower_slope, lower_intercept, fit_end))
            ) / (2 * atr)
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
            state, breakout_direction, breakout_index, confirmation = _breakout_state(
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
                and channel_width_atr <= 2.5
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
                "confirmation": confirmation,
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
                "channelWidthAtr": round(channel_width_atr, 4),
                "spanBars": pole_end - start + flag_bars,
                "geometry": geometry,
                "evidenceRefs": [str(first.get("id")), str(second.get("id"))],
                "rejectReasons": reasons,
            })
    return results


def _continuation_candidates(candles, pivots, *, atr, interval):
    results = []
    for impulse in _impulse_pairs(candles, pivots, atr=atr):
        first, second = impulse["first"], impulse["second"]
        start, pole_end = impulse["start"], impulse["poleEnd"]
        direction, pole_move, efficiency = impulse["direction"], impulse["poleMove"], impulse["efficiency"]
        for fit_end in dict.fromkeys((len(candles) - 1, len(candles) - 2)):
            pattern_start = pole_end + 1
            pattern_bars = fit_end - pattern_start + 1
            if not 5 <= pattern_bars <= 40:
                continue
            rows = candles[pattern_start:fit_end + 1]
            upper_points = [(pattern_start + index, float(row["high"])) for index, row in enumerate(rows)]
            lower_points = [(pattern_start + index, float(row["low"])) for index, row in enumerate(rows)]
            upper_slope, upper_intercept = _fit_xy(upper_points)
            lower_slope, lower_intercept = _fit_xy(lower_points)
            upper_slope_atr, lower_slope_atr = upper_slope / atr, lower_slope / atr
            starting_width = _line(upper_slope, upper_intercept, pattern_start) - _line(lower_slope, lower_intercept, pattern_start)
            ending_width = _line(upper_slope, upper_intercept, fit_end) - _line(lower_slope, lower_intercept, fit_end)
            convergence = ending_width / max(starting_width, 1e-12)
            is_pennant = upper_slope_atr <= -0.01 and lower_slope_atr >= 0.01
            is_rectangle = abs(upper_slope_atr) <= 0.025 and abs(lower_slope_atr) <= 0.025
            if not is_pennant and not is_rectangle:
                continue
            family = "pennant" if is_pennant else "rectangle"
            kind = f"{'bullish' if direction > 0 else 'bearish'}_{family}"
            upper_contacts = _contact_count(upper_points, upper_slope, upper_intercept, atr, interval)
            lower_contacts = _contact_count(lower_points, lower_slope, lower_intercept, atr, interval)
            containment = _containment(
                candles, pattern_start, fit_end,
                upper_slope, upper_intercept, lower_slope, lower_intercept, atr,
            )
            if direction > 0:
                retracement = (float(second["price"]) - min(float(row["low"]) for row in rows)) / pole_move
            else:
                retracement = (max(float(row["high"]) for row in rows) - float(second["price"])) / pole_move
            state, breakout_direction, _breakout_index, confirmation = _breakout_state(
                candles,
                upper_slope=upper_slope,
                upper_intercept=upper_intercept,
                lower_slope=lower_slope,
                lower_intercept=lower_intercept,
                atr=atr,
                expected="up" if direction > 0 else "down",
                inspect_from=fit_end + 1,
            )
            family_geometry = (
                TRIANGLE_MIN_CONVERGENCE <= convergence <= TRIANGLE_MAX_CONVERGENCE
                if is_pennant
                else 0.85 <= convergence <= 1.15
            )
            evidence_pass = (
                0.10 <= retracement <= 0.60
                and starting_width >= 2 * atr
                and family_geometry
                and upper_contacts >= 2
                and lower_contacts >= 2
                and containment >= 0.80
            )
            active_pass = state in {"forming", "confirmed"}
            hard_pass = evidence_pass and active_pass
            score = (
                0.20 * min(1.0, pole_move / (8 * atr))
                + 0.15 * efficiency
                + 0.20 * containment
                + 0.15 * min(1.0, (upper_contacts + lower_contacts) / 6)
                + 0.15 * (1 - min(1.0, abs(retracement - 0.35) / 0.35))
                + 0.15 * (1 if state == "confirmed" else 0.8)
            )
            geometry = {
                "pole": {
                    "start": _point(candles[start], float(first["price"])),
                    "end": _point(candles[pole_end], float(second["price"])),
                },
                "upper": _boundary(candles, pattern_start, fit_end, upper_slope, upper_intercept),
                "lower": _boundary(candles, pattern_start, fit_end, lower_slope, lower_intercept),
            }
            reasons = []
            if not evidence_pass:
                reasons.append(f"{family}_geometry")
            if state == "invalidated":
                reasons.append("opposite_breakout")
            raw = f"{interval}|{kind}|{start}|{pole_end}|{fit_end}|{geometry}"
            results.append({
                "id": f"{interval}:pattern:{hashlib.sha256(raw.encode()).hexdigest()[:10]}",
                "kind": kind,
                "state": state,
                "breakoutDirection": breakout_direction,
                "confirmation": confirmation,
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
                "convergenceRatio": round(convergence, 4),
                "spanBars": pole_end - start + pattern_bars,
                "geometry": geometry,
                "evidenceRefs": [str(first.get("id")), str(second.get("id"))],
                "rejectReasons": reasons,
            })
    return results


def _sloped_boundary_candidates(candles, pivots, *, atr, interval):
    candidates = [
        candidate
        for highs, lows, search_span in _triangle_pivot_groups(pivots, len(candles))
        if (candidate := _sloped_boundary_candidate(
            candles, highs, lows, atr=atr, interval=interval, search_span=search_span,
        )) is not None
    ]
    passed = sorted((item for item in candidates if item["hardPass"]), key=lambda item: (-float(item["score"]), item["id"]))
    rejected = sorted((item for item in candidates if not item["hardPass"]), key=lambda item: (-float(item["score"]), item["id"]))
    return [*passed[:TRIANGLE_RETAINED_PER_STATE], *rejected[:TRIANGLE_RETAINED_PER_STATE]]


def _sloped_boundary_candidate(candles, highs, lows, *, atr, interval, search_span):
    if len(highs) < 2 or len(lows) < 2 or len(highs) + len(lows) < 5:
        return None
    upper_slope, upper_intercept = _fit_points(highs)
    lower_slope, lower_intercept = _fit_points(lows)
    upper_slope_atr, lower_slope_atr = upper_slope / atr, lower_slope / atr
    parallel_error = abs(upper_slope_atr - lower_slope_atr)
    directional = 0.01
    if upper_slope_atr <= -directional and lower_slope_atr <= -directional and parallel_error <= 0.025:
        kind, expected, family = "descending_channel_breakout", "up", "channel"
    elif upper_slope_atr >= directional and lower_slope_atr >= directional and parallel_error <= 0.025:
        kind, expected, family = "ascending_channel_breakdown", "down", "channel"
    elif lower_slope_atr > upper_slope_atr >= directional:
        kind, expected, family = "rising_wedge", "down", "wedge"
    elif upper_slope_atr < lower_slope_atr <= -directional:
        kind, expected, family = "falling_wedge", "up", "wedge"
    else:
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
    width_ratio = current_width / max(starting_width, 1e-12)
    residuals = [
        abs(float(item["price"]) - _line(upper_slope, upper_intercept, int(item["barIndex"]))) / atr
        for item in highs
    ] + [
        abs(float(item["price"]) - _line(lower_slope, lower_intercept, int(item["barIndex"]))) / atr
        for item in lows
    ]
    state, breakout_direction, breakout_index, confirmation = _breakout_state(
        candles,
        upper_slope=upper_slope,
        upper_intercept=upper_intercept,
        lower_slope=lower_slope,
        lower_intercept=lower_intercept,
        atr=atr,
        expected=expected,
    )
    containment = _containment(
        candles, start_index, breakout_index - 1 if breakout_index is not None else end_index,
        upper_slope, upper_intercept, lower_slope, lower_intercept, atr,
    )
    family_geometry = (
        TRIANGLE_MIN_CONVERGENCE <= width_ratio <= TRIANGLE_MAX_CONVERGENCE
        if family == "wedge"
        else 0.75 <= width_ratio <= 1.25 and parallel_error <= 0.025
    )
    evidence_pass = (
        max(residuals, default=99.0) <= TRIANGLE_MAX_RESIDUAL_ATR
        and starting_width >= 2 * atr
        and family_geometry
        and containment >= TRIANGLE_MIN_CONTAINMENT
    )
    active_pass = state in {"forming", "confirmed"} if family == "wedge" else state == "confirmed"
    hard_pass = evidence_pass and active_pass
    score = (
        0.25 * min(1.0, (len(highs) + len(lows)) / 6)
        + 0.25 * containment
        + 0.20 * (1 - min(1.0, max(residuals, default=1.0) / TRIANGLE_MAX_RESIDUAL_ATR))
        + 0.15 * (1 - min(1.0, parallel_error / 0.10))
        + 0.15 * (1 if state == "confirmed" else 0.8)
    )
    geometry = {
        "upper": _boundary(candles, start_index, end_index, upper_slope, upper_intercept),
        "lower": _boundary(candles, start_index, end_index, lower_slope, lower_intercept),
    }
    evidence_refs = [str(item.get("id")) for item in (*highs, *lows)]
    raw = f"{interval}|{kind}|{start_index}|{end_index}|{evidence_refs}|{geometry}"
    reasons = []
    if not evidence_pass:
        reasons.append(f"{family}_geometry")
    if state == "invalidated":
        reasons.append("opposite_breakout")
    elif family == "channel" and state != "confirmed":
        reasons.append("breakout_unconfirmed")
    return {
        "id": f"{interval}:pattern:{hashlib.sha256(raw.encode()).hexdigest()[:10]}",
        "kind": kind,
        "state": state,
        "breakoutDirection": breakout_direction,
        "confirmation": confirmation,
        "hardPass": hard_pass,
        "evidencePass": evidence_pass,
        "activePass": active_pass,
        "score": round(max(0.0, min(1.0, score)), 4),
        "touches": len(highs) + len(lows),
        "upperTouches": len(highs),
        "lowerTouches": len(lows),
        "containment": round(containment, 4),
        "convergenceRatio": round(width_ratio, 4),
        "parallelSlopeErrorAtr": round(parallel_error, 4),
        "maxResidualAtr": round(max(residuals, default=0.0), 4),
        "spanBars": end_index - start_index,
        "searchWindowBars": search_span,
        "geometry": geometry,
        "evidenceRefs": evidence_refs,
        "rejectReasons": reasons,
    }


def _impulse_pairs(candles, pivots, *, atr):
    ordered = sorted(pivots, key=lambda item: int(item.get("barIndex", -1)))
    results = []
    for first, second in zip(ordered, ordered[1:]):
        start = int(first.get("barIndex", -1))
        pole_end = int(second.get("barIndex", -1))
        if not 3 <= pole_end - start <= 20:
            continue
        if (first.get("kind"), second.get("kind")) == ("L", "H"):
            direction = 1
        elif (first.get("kind"), second.get("kind")) == ("H", "L"):
            direction = -1
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
        results.append({
            "first": first,
            "second": second,
            "start": start,
            "poleEnd": pole_end,
            "direction": direction,
            "poleMove": pole_move,
            "efficiency": efficiency,
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
        return "forming", None, None, None
    index, direction = detected
    if expected not in {"either", direction}:
        return "invalidated", direction, index, None
    baseline_volumes = [float(row.get("volume") or 0) for row in candles[max(0, index - 20):index]]
    median_volume = statistics.median(baseline_volumes) if baseline_volumes else 0.0
    held = index + 1 < len(candles) and (
        float(candles[index + 1]["close"]) > _line(upper_slope, upper_intercept, index + 1) + 0.25 * atr
        if direction == "up"
        else float(candles[index + 1]["close"]) < _line(lower_slope, lower_intercept, index + 1) - 0.25 * atr
    )
    volume_confirmed = median_volume > 0 and float(candles[index].get("volume") or 0) >= 1.5 * median_volume
    if held or volume_confirmed:
        boundary = _line(
            upper_slope if direction == "up" else lower_slope,
            upper_intercept if direction == "up" else lower_intercept,
            index,
        )
        close = float(candles[index]["close"])
        relative_volume = float(candles[index].get("volume") or 0) / median_volume if median_volume > 0 else None
        mode = "both" if held and volume_confirmed else "next_close_hold" if held else "relative_volume"
        return "confirmed", direction, index, {
            "breakoutAt": str(candles[index]["timestamp"]),
            "confirmedAt": str(candles[index + 1]["timestamp"] if held else candles[index]["timestamp"]),
            "mode": mode,
            "boundaryPrice": round(boundary, 6),
            "penetrationAtr": round(abs(close - boundary) / max(float(atr), 1e-12), 6),
            "relativeVolume": round(relative_volume, 6) if relative_volume is not None else None,
        }
    return "forming", None, None, None


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
