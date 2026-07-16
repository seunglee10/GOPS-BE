from __future__ import annotations

import hashlib
import statistics
from typing import Any

from alfaka.serving.volume_profile import compute_volume_profile_payload

from .atr import latest_atr as regression_atr
from .config import QUALITY_CONFIG
from .levels import compute_levels
from .patterns import TRIANGLE_KINDS, compute_patterns, compute_triangles
from .pivots import compute_pivots
from .trade_timing import evaluate_pattern_trade_timing
from .trends import compute_trends


SUPPORTED_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")
TARGET_BARS = {**{interval: 380 for interval in SUPPORTED_INTERVALS[:-1]}, "1W": 312}
WARMUP_BARS = {interval: 120 for interval in SUPPORTED_INTERVALS}
EVALUATION_BARS = {**{interval: 260 for interval in SUPPORTED_INTERVALS[:-1]}, "1W": 192}
MINIMUM_BARS = 120
ALGORITHM_VERSION = "ohlcv-consensus-pattern-families-v6"

_ATR_PERIOD = 14
_VOLUME_BASELINE = 20
_TRACE_PIVOT_REFERENCE_FIELDS = (
    "evidenceRefs", "anchorPivotIds", "touchPivotIds", "reactionPivotIds",
)


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
    display_from = str(rows[evaluation_from]["timestamp"])
    pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    latest_atr = max(atr_values[-1], 1e-12)
    current = float(rows[-1]["close"])
    supports, resistances, level_candidates = _confirmed_horizontal_levels(
        symbol, interval, rows, current=current, atr=latest_atr, pivots=pivots,
    )
    trace_pattern_candidates = _regression_pattern_candidates(
        rows,
        interval=interval,
        pivots=pivots,
        retain_competitors=True,
    )
    pattern_candidates = _public_pattern_candidate_slice(trace_pattern_candidates)
    active_patterns = sorted(
        (item for item in pattern_candidates if item["hardPass"] and item["state"] in {"forming", "confirmed"}),
        key=lambda item: (-item["score"], -item["endIndex"], item["geometryHash"]),
    )
    primary_pattern = active_patterns[0] if active_patterns else None
    public_primary_pattern = _public_pattern(primary_pattern)
    trade_plan = evaluate_pattern_trade_timing(
        rows,
        public_primary_pattern,
        atr=latest_atr,
        symbol=symbol,
        interval=interval,
    )
    triangle_candidates = _regression_triangle_candidates(rows, interval=interval, pivots=pivots)
    active = sorted(
        (item for item in triangle_candidates if item["hardPass"] and item["state"] in {"forming", "confirmed"}),
        key=lambda item: (-item["score"], -item["endIndex"], item["geometryHash"]),
    )
    primary = active[0] if active else None
    historical = next(
        (item for item in sorted(triangle_candidates, key=lambda item: (-item["score"], -item["endIndex"], item["geometryHash"])) if not primary or item["geometryHash"] != primary["geometryHash"]),
        None,
    )
    trend_candidates = compute_trends(
        rows,
        pivots,
        display_from=display_from,
        atr=latest_atr,
        interval=interval,
        retain_competitors=True,
    )
    primary_trend_candidate = next(
        (
            item
            for item in trend_candidates
            if item.get("hardPass") and item.get("kind") in {"up", "down", "channel"}
        ),
        None,
    )
    generated_at = str(rows[-1]["timestamp"])
    level_drawings = [
        _level_drawing(symbol, interval, item, generated_at)
        for item in (*supports, *resistances)
    ]
    pattern_drawings = _pattern_drawings(
        symbol, interval, primary_pattern, generated_at,
    ) if primary_pattern else []
    public_primary_trend = _public_trend(
        symbol,
        interval,
        primary_trend_candidate,
        pivots=pivots,
        atr=latest_atr,
    )
    trend_drawings = [
        _trend_drawing(symbol, interval, public_primary_trend, generated_at)
    ] if public_primary_trend else []
    if len(level_drawings) > 4 or len(pattern_drawings) > 3 or len(trend_drawings) > 1:
        raise ValueError("Geometry drawing group exceeds its atomic budget")
    drawings = [*level_drawings, *pattern_drawings, *trend_drawings]
    if len(drawings) > 8:
        raise ValueError("Geometry drawing budget exceeded")
    drawing_groups = {
        "levels": [item["id"] for item in level_drawings],
        "trend": [item["id"] for item in trend_drawings],
        "pattern": [item["id"] for item in pattern_drawings],
    }
    analysis_trace = _analysis_trace(
        rows,
        pivots=pivots,
        level_candidates=level_candidates,
        selected_levels=[*supports, *resistances],
        trend_candidates=trend_candidates,
        primary_trend=public_primary_trend,
        pattern_candidates=trace_pattern_candidates,
        primary_pattern=primary_pattern,
        atr=latest_atr,
    )
    return {
        "algorithmVersion": ALGORITHM_VERSION,
        "supports": supports,
        "resistances": resistances,
        "patterns": [_public_pattern(item) for item in active_patterns[:8]],
        "primaryPattern": public_primary_pattern,
        "tradePlan": trade_plan,
        "primaryTriangle": _public_triangle(primary),
        "historicalTriangle": _public_triangle(historical),
        "trends": [public_primary_trend] if public_primary_trend else [],
        "primaryTrend": public_primary_trend,
        "indicators": compute_sma_snapshot(rows),
        "drawings": drawings,
        "drawingGroups": drawing_groups,
        "analysisTrace": analysis_trace,
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
                previous_difference = previous_short - previous_long
                current_difference = current_short - current_long
                difference_change = current_difference - previous_difference
                fraction = max(0.0, min(1.0, -previous_difference / difference_change))
                short_cross = previous_short + fraction * (current_short - previous_short)
                long_cross = previous_long + fraction * (current_long - previous_long)
                events.append({
                    "status": "crossed", "direction": direction,
                    "timestamp": candles[index]["timestamp"], "barsAgo": len(closes) - 1 - index,
                    "previousTimestamp": candles[index - 1]["timestamp"],
                    "fraction": round(fraction, 9),
                    "price": round((short_cross + long_cross) / 2, 6),
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


def _confirmed_horizontal_levels(symbol, interval, rows, *, current, atr, pivots=None):
    """Select role-local levels while retaining every considered candidate."""
    if pivots is None:
        display_from = rows[max(0, len(rows) - EVALUATION_BARS[interval])]["timestamp"]
        pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    profile_rows = rows[-WARMUP_BARS[interval]:]
    volume_profile = compute_volume_profile_payload(
        profile_rows,
        symbol=symbol,
        interval=interval,
        from_time=str(profile_rows[0]["timestamp"]),
        to_time=str(profile_rows[-1]["timestamp"]),
        target_bins=24,
    )
    candidates = compute_levels(
        rows,
        pivots,
        atr=atr,
        volume_profile=volume_profile,
        expected_bars=TARGET_BARS[interval],
        interval=interval,
        rejected_limit=None,
    )
    evidence_by_id = {str(item["id"]): item for item in pivots}
    annotated = []
    for candidate in candidates:
        role = candidate["role"]
        role_side_pass = (
            role == "support" and current >= float(candidate["zoneLow"]) - 0.25 * atr
        ) or (
            role == "resistance" and current <= float(candidate["zoneHigh"]) + 0.25 * atr
        )
        selection_tier = (
            "confirmed" if candidate["hardPass"]
            else "contextual" if role_side_pass and _contextual_level_pass(candidate, interval)
            else "reference" if role_side_pass and _reference_level_pass(candidate, interval)
            else None
        )
        annotated.append({
            **candidate,
            "selectionTier": selection_tier,
            "roleSidePass": role_side_pass,
            "selected": False,
            "importanceTier": None,
            "importanceRank": None,
        })

    selected = []
    for role in ("support", "resistance"):
        role_candidates = [
            item for item in annotated
            if item["role"] == role and item["roleSidePass"] and item["selectionTier"] is not None
        ]
        confirmed = [item for item in role_candidates if item["selectionTier"] == "confirmed"]
        contextual = [item for item in role_candidates if item["selectionTier"] == "contextual"]
        reference = [item for item in role_candidates if item["selectionTier"] == "reference"]
        if confirmed:
            role_selected = _non_overlapping_levels(confirmed, limit=2)
        elif contextual:
            role_selected = _non_overlapping_levels(contextual, limit=2)
        else:
            role_selected = _non_overlapping_levels(reference, limit=2)
        for rank, candidate in enumerate(role_selected, start=1):
            candidate["selected"] = True
            candidate["importanceRank"] = rank
            candidate["importanceTier"] = (
                "major" if candidate["selectionTier"] == "confirmed" and rank == 1
                else "standard" if candidate["selectionTier"] in {"confirmed", "contextual"}
                else "minor"
            )
        selected.extend(role_selected)

    public_levels = []
    for candidate in selected:
        role = candidate["role"]
        price = float(candidate["price"])
        projected = {
            "id": candidate["id"],
            "role": role,
            "selectionTier": candidate["selectionTier"],
            "importanceTier": candidate["importanceTier"],
            "importanceRank": candidate["importanceRank"],
            "price": price,
            "zoneLow": candidate["zoneLow"],
            "zoneHigh": candidate["zoneHigh"],
            "halfWidthAtr": candidate["halfWidthAtr"],
            "score": candidate["score"],
            "touches": candidate["touches"],
            "reactionCount": candidate["reactionCount"],
            "lastTouchAgeBars": candidate["lastTouchAgeBars"],
            "currentDistanceAtr": candidate["currentDistanceAtr"],
            "state": candidate["state"],
            "evidencePass": candidate["evidencePass"],
            "activePass": candidate["activePass"],
            "hardPass": candidate["hardPass"],
            "roleFlips": candidate["roleFlips"],
            "vpConfluence": candidate["vpConfluence"],
            "roundNumber": candidate["roundNumber"],
            "anchors": [
                {"timestamp": candidate["firstTestAt"], "price": price},
                {"timestamp": candidate["lastTestAt"], "price": price},
            ],
            "evidence": [
                evidence_by_id[pivot_id]
                for pivot_id in candidate["memberPivotIds"]
                if pivot_id in evidence_by_id
            ][:8],
        }
        public_levels.append(projected)
    supports = [item for item in public_levels if item["role"] == "support"]
    resistances = [item for item in public_levels if item["role"] == "resistance"]
    return supports, resistances, annotated


def _contextual_level_pass(candidate, interval):
    config = QUALITY_CONFIG[interval]
    role = candidate.get("role")
    return (
        role in {"support", "resistance"}
        and candidate.get("state") in {f"{role}_active", f"role_flip_{role}"}
        and int(candidate.get("touches") or 0) >= 3
        and int(candidate.get("reactionCount") or 0) >= 1
        and int(candidate.get("lastTouchAgeBars", 10**9)) <= config.level_last_touch_max_age
        and float(candidate.get("currentDistanceAtr", 10**9)) <= 3
    )


def _reference_level_pass(candidate, interval):
    config = QUALITY_CONFIG[interval]
    role = candidate.get("role")
    return (
        role in {"support", "resistance"}
        and candidate.get("state") in {f"{role}_active", f"role_flip_{role}"}
        and len(_unique_strings(candidate.get("memberPivotIds", []))) >= 2
        and int(candidate.get("touches") or 0) >= 2
        and int(candidate.get("reactionCount") or 0) >= 1
        and int(candidate.get("lastTouchAgeBars", 10**9)) <= config.level_last_touch_max_age
        and float(candidate.get("currentDistanceAtr", 10**9)) <= 4
        and "role_conflict" not in candidate.get("rejectReasons", [])
        and "break_pending" not in candidate.get("rejectReasons", [])
    )


def _level_selection_key(candidate):
    tier_rank = {"confirmed": 0, "contextual": 1, "reference": 2}
    return (
        tier_rank.get(candidate.get("selectionTier"), 99),
        -float(candidate.get("score") or 0),
        -int(candidate.get("reactionCount") or 0),
        -int(candidate.get("touches") or 0),
        int(candidate.get("lastTouchAgeBars", 10**9)),
        float(candidate.get("price") or 0),
        str(candidate.get("id") or ""),
    )


def _non_overlapping_levels(candidates, *, limit, sort_key=_level_selection_key):
    selected = []
    for candidate in sorted(candidates, key=sort_key):
        overlaps = any(
            max(float(candidate["zoneLow"]), float(existing["zoneLow"]))
            <= min(float(candidate["zoneHigh"]), float(existing["zoneHigh"]))
            for existing in selected
        )
        if overlaps:
            continue
        selected.append(candidate)
        if len(selected) == limit:
            break
    return selected


def _regression_triangle_candidates(
    rows: list[dict[str, Any]], *, interval: str, pivots=None,
) -> list[dict[str, Any]]:
    if pivots is None:
        display_from = rows[max(0, len(rows) - EVALUATION_BARS[interval])]["timestamp"]
        pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    candidates = compute_triangles(rows, pivots, atr=regression_atr(rows), interval=interval)
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


def _regression_pattern_candidates(
    rows: list[dict[str, Any]], *, interval: str, pivots=None,
    retain_competitors: bool = False,
) -> list[dict[str, Any]]:
    if pivots is None:
        display_from = rows[max(0, len(rows) - EVALUATION_BARS[interval])]["timestamp"]
        pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    candidates = compute_patterns(
        rows,
        pivots,
        atr=regression_atr(rows),
        interval=interval,
        retain_competitors=retain_competitors,
    )
    evidence_by_id = {str(item["id"]): item for item in pivots}
    index_by_timestamp = {str(row["timestamp"]): index for index, row in enumerate(rows)}
    results = []
    for candidate in candidates:
        geometry = candidate["geometry"]
        geometry_hash = hashlib.sha256(str(candidate["id"]).encode()).hexdigest()[:16]
        end_index = max(
            (
                index_by_timestamp.get(str(point.get("timestamp")), len(rows) - 1)
                for boundary in geometry.values()
                if isinstance(boundary, dict)
                for point in boundary.values()
                if isinstance(point, dict)
            ),
            default=len(rows) - 1,
        )
        projected = {
            **candidate,
            "endIndex": end_index,
            "geometryHash": geometry_hash,
            "evidence": [evidence_by_id[ref] for ref in candidate.get("evidenceRefs", []) if ref in evidence_by_id],
        }
        for name in ("pole", "upper", "lower"):
            if name in geometry:
                projected[name] = geometry[name]
        if candidate["kind"] in TRIANGLE_KINDS:
            projected["apexBarsFromAsOf"] = _pattern_apex_bars(geometry, index_by_timestamp, end_index)
        results.append(projected)
    return results


def _public_pattern_candidate_slice(candidates):
    """Recreate compute_patterns' stable public projection from diagnostics."""
    passed = [item for item in candidates if item.get("hardPass")]
    rejected = [item for item in candidates if not item.get("hardPass")]
    return [*passed[:1], *rejected[:3]]


def _level_drawing(symbol, interval, level, generated_at):
    color = "#22c55e" if level["role"] == "support" else "#ef4444"
    importance = level.get("importanceTier")
    contextual = level.get("selectionTier") == "contextual"
    base_label = "지지" if level["role"] == "support" else "저항"
    if importance == "major":
        label, width, opacity, dash, line_style = base_label, 3.0, 0.95, [], "solid"
    elif importance == "standard":
        label, width, opacity, dash, line_style = f"보조 {base_label}", 2.25, 0.72, [6, 4], "dashed"
    elif importance == "minor":
        label, width, opacity, dash, line_style = f"참고 {base_label}", 1.5, 0.45, [2, 4], "dashed"
    else:
        # Old assets did not carry an importance tier. Preserve their raw
        # geometry style so the reader can apply the legacy 2.5px treatment.
        label = f"보조 {base_label}" if contextual else base_label
        width, opacity, dash, line_style = 2, 0.72 if contextual else 0.86, [6, 4], "dashed"
    drawing = _drawing(
        symbol, interval, level["id"], "horizontalLine", level["anchors"], color,
        label, generated_at, opacity=opacity,
    )
    drawing["style"] = {
        **drawing["style"],
        "lineWidth": width,
        "lineDash": dash,
        "lineStyle": line_style,
    }
    return drawing


def _public_trend(symbol, interval, candidate, *, pivots, atr):
    if candidate is None or candidate.get("kind") not in {"up", "down", "channel"}:
        return None
    pivot_by_id = {str(item["id"]): item for item in pivots}
    required = 3 if candidate["kind"] == "channel" else 2
    anchor_ids = [
        str(value) for value in candidate.get("anchorPivotIds", [])
        if str(value) in pivot_by_id
    ][:required]
    if len(anchor_ids) != required:
        return None
    anchors = [
        {
            "timestamp": pivot_by_id[pivot_id]["timestamp"],
            "price": round(float(pivot_by_id[pivot_id]["price"]), 6),
        }
        for pivot_id in anchor_ids
    ]
    direction = str(candidate.get("direction") or candidate["kind"])
    public_kind = "channel" if candidate["kind"] == "channel" else f"{candidate['kind']}trend"
    reaction_bars = {
        int(item["barIndex"])
        for item in candidate.get("touchEpisodes", [])
        if item.get("reactionPass")
    }
    touch_pivot_ids = _unique_strings(candidate.get("touchPivotIds", []))
    reaction_pivot_ids = [
        str(item["id"]) for item in pivots
        if int(item["barIndex"]) in reaction_bars and str(item["id"]) in touch_pivot_ids
    ]
    drawing_id = f"chart-asset:{symbol.upper()}:{interval}:{candidate['id']}"
    result = {
        "id": candidate["id"],
        "kind": public_kind,
        "direction": direction,
        "score": candidate["score"],
        "drawingId": drawing_id,
        "anchors": anchors,
        "anchorPivotIds": anchor_ids,
        "touchPivotIds": touch_pivot_ids,
        "reactionPivotIds": _unique_strings(reaction_pivot_ids),
        "touchCount": int(candidate.get("touches") or 0),
        "reactionCount": int(candidate.get("reactionCount") or 0),
        "slopeAtrPerBar": round(float(candidate.get("slopeAtrPerBar") or 0), 6),
        "medianResidualAtr": round(float(candidate.get("medianResidualAtr") or 0), 6),
        "currentDistanceAtr": round(float(candidate.get("currentDistanceAtr") or 0), 6),
        "lastTouchAgeBars": int(candidate.get("lastTouchAgeBars") or 0),
        "activeInvalidation": bool(candidate.get("activeInvalidation")),
        "violationCount": int(candidate.get("violationCount") or 0),
        "invalidation": (
            "active" if candidate.get("activeInvalidation")
            else "historical_revalidated" if int(candidate.get("violationCount") or 0) > 0
            else None
        ),
    }
    if candidate["kind"] == "channel":
        result.update({
            "channelWidthAtr": round(float(candidate.get("channelWidth") or 0) / max(atr, 1e-12), 6),
            "parallelSlopeError": round(float(candidate.get("parallelSlopeError") or 0), 6),
            "containment": round(float(candidate.get("containment") or 0), 6),
        })
    return result


def _trend_drawing(symbol, interval, trend, generated_at):
    color = "#22c55e" if trend["direction"] == "up" else "#ef4444"
    name = (
        f"{'상승' if trend['direction'] == 'up' else '하락'} 평행 채널"
        if trend["kind"] == "channel"
        else f"{'상승' if trend['direction'] == 'up' else '하락'} 추세선"
    )
    drawing = _drawing(
        symbol,
        interval,
        trend["id"],
        "trendParallelLines" if trend["kind"] == "channel" else "trendLine",
        trend["anchors"],
        color,
        name,
        generated_at,
        opacity=0.86,
    )
    drawing["style"] = {
        **drawing["style"],
        "lineWidth": 2.75,
        "lineDash": [],
        "lineStyle": "solid",
        "extension": "ray",
    }
    if trend["kind"] == "channel":
        drawing["parallelLineCount"] = 2
    return drawing


def _pattern_drawings(symbol, interval, pattern, generated_at):
    color = _pattern_color(pattern["kind"])
    name = _pattern_name(pattern["kind"])
    state = "돌파 확인" if pattern["state"] == "confirmed" else "형성 중"
    opacity = 0.95 if pattern["state"] == "confirmed" else 0.58
    drawings = []
    if pattern.get("pole"):
        drawings.append(_drawing(
            symbol, interval, f"{pattern['geometryHash']}-pole", "trendLine",
            [pattern["pole"]["start"], pattern["pole"]["end"]], color,
            f"{name} · 깃대", generated_at, opacity=opacity,
        ))
    for boundary in ("upper", "lower"):
        if not pattern.get(boundary):
            continue
        drawings.append(_drawing(
            symbol, interval, f"{pattern['geometryHash']}-{boundary}", "trendLine",
            [pattern[boundary]["start"], pattern[boundary]["end"]], color,
            f"{name} · {state}", generated_at, opacity=opacity,
        ))
    return drawings


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
        "apexBarsFromAsOf", "evidence", "confirmation",
    )}


def _public_pattern(value):
    if value is None:
        return None
    keys = (
        "id", "kind", "state", "breakoutDirection", "score", "touches", "upperTouches", "lowerTouches",
        "containment", "convergenceRatio", "parallelSlopeErrorAtr", "maxResidualAtr", "poleAtr",
        "poleEfficiency", "retracementRatio", "channelWidthAtr", "geometryHash", "pole", "upper", "lower",
        "apexBarsFromAsOf", "evidence", "confirmation",
    )
    return {
        **{key: value[key] for key in keys if key in value},
        "bias": _pattern_bias(value["kind"]),
    }


def _analysis_trace(
    rows,
    *,
    pivots,
    level_candidates,
    selected_levels,
    trend_candidates,
    primary_trend,
    pattern_candidates,
    primary_pattern,
    atr,
):
    selected_level_ids = {str(item["id"]) for item in selected_levels}
    selected_trend_ids = {str(primary_trend["id"])} if primary_trend else set()
    selected_pattern_ids = {str(primary_pattern["id"])} if primary_pattern else set()
    diagonal_trends = [
        item for item in trend_candidates if item.get("kind") in {"up", "down", "channel"}
    ]
    kept_levels = _retain_trace_candidates(level_candidates, selected_level_ids)
    kept_trends = _retain_trace_candidates(diagonal_trends, selected_trend_ids)
    kept_patterns = _retain_trace_candidates(pattern_candidates, selected_pattern_ids)
    pivot_by_id = {str(item["id"]): item for item in pivots}
    level_trace, level_touch_omitted = _project_trace_group(
        kept_levels,
        lambda item, rank: _trace_level_candidate(
            rows, pivots, item, selected=str(item["id"]) in selected_level_ids,
            category_rank=rank,
        ),
    )
    trend_trace, trend_touch_omitted = _project_trace_group(
        kept_trends,
        lambda item, rank: _trace_trend_candidate(
            rows,
            pivots,
            item,
            selected=str(item["id"]) in selected_trend_ids,
            atr=atr,
            category_rank=rank,
        ),
    )
    pattern_trace, pattern_touch_omitted = _project_trace_group(
        kept_patterns,
        lambda item, rank: _trace_pattern_candidate(
            item,
            pivot_by_id,
            selected=str(item["id"]) in selected_pattern_ids,
            category_rank=rank,
        ),
    )
    referenced = {
        str(reference)
        for candidate in (*level_trace, *trend_trace, *pattern_trace)
        for reference_field in _TRACE_PIVOT_REFERENCE_FIELDS
        for reference in candidate[reference_field]
    }
    trace_pivots = [
        _trace_pivot(item)
        for item in pivots
        if str(item["id"]) in referenced
    ]
    trace = {
        "version": "geometry-analysis-trace-v2",
        "pivots": trace_pivots,
        "levelCandidates": level_trace,
        "trendCandidates": trend_trace,
        "patternCandidates": pattern_trace,
        "selections": {
            "levelCandidateIds": sorted(selected_level_ids),
            "trendCandidateIds": sorted(selected_trend_ids),
            "patternCandidateIds": sorted(selected_pattern_ids),
        },
        "omittedCounts": {
            "levelCandidates": 0,
            "trendCandidates": 0,
            "patternCandidates": 0,
            "touchEpisodes": level_touch_omitted + trend_touch_omitted + pattern_touch_omitted,
        },
        "completeness": {
            "complete": True,
            "detected": {
                "levels": len(level_candidates),
                "trends": len(diagonal_trends),
                "patterns": len(pattern_candidates),
            },
            "stored": {
                "levels": len(level_trace),
                "trends": len(trend_trace),
                "patterns": len(pattern_trace),
            },
        },
    }
    return trace


def _retain_trace_candidates(candidates, selected_ids):
    # Detectors already provide their deterministic ranking (including pattern
    # family priority and channel-first trend competition). Move selected
    # candidates to the front without disturbing either partition's order.
    selected = [item for item in candidates if str(item.get("id")) in selected_ids]
    unselected = [item for item in candidates if str(item.get("id")) not in selected_ids]
    ordered = [*selected, *unselected]
    return ordered


def _project_trace_group(candidates, projector):
    projected = []
    omitted = 0
    for rank, candidate in enumerate(candidates, start=1):
        item, item_omitted = projector(candidate, rank)
        projected.append(item)
        omitted += item_omitted
    return projected, omitted


def _trace_level_candidate(rows, pivots, candidate, *, selected, category_rank):
    touch_records = []
    episodes = list(candidate.get("touchEpisodes", []))
    for episode in episodes:
        index = int(episode.get("startIndex", -1))
        if not 0 <= index < len(rows):
            continue
        timestamp = str(rows[index]["timestamp"])
        approach = str(episode.get("approach") or "inside")
        price_key = "low" if approach == "above" else "high" if approach == "below" else "close"
        touch_records.append(_trace_touch(
            candidate["id"],
            timestamp=timestamp,
            bar_index=index,
            price=float(rows[index][price_key]),
            outcome=episode.get("outcome"),
            boundary=candidate.get("role") or approach,
            mfe_atr=episode.get("mfeAtr"),
            mae_atr=episode.get("maeAtr"),
        ))
    touch_refs = [item["id"] for item in touch_records]
    reaction_refs = [
        item["id"] for item in touch_records if item.get("outcome") == "reaction"
    ]
    pivot_by_id = {str(item["id"]): item for item in pivots}
    evidence_refs = [
        reference for reference in _unique_strings(candidate.get("memberPivotIds", []))
        if reference in pivot_by_id
    ]
    ordered_evidence = sorted(
        evidence_refs,
        key=lambda reference: (int(pivot_by_id[reference]["barIndex"]), reference),
    )
    reaction_bars = {
        int(episode.get("startIndex", -1))
        for episode in episodes
        if episode.get("outcome") == "reaction"
    }
    reaction_pivot_ids = [
        reference for reference in ordered_evidence
        if int(pivot_by_id[reference]["barIndex"]) in reaction_bars
    ]
    raw_reasons = _trace_reject_reasons(candidate, selected=selected)
    if (
        not candidate.get("hardPass")
        and candidate.get("selectionTier") is None
        and len(_unique_strings(candidate.get("memberPivotIds", []))) < 2
        and "single_swing" not in raw_reasons
    ):
        raw_reasons.append("single_swing")
    result = {
        "id": candidate["id"],
        "category": "level",
        "score": candidate.get("score", 0),
        "selected": selected,
        "hardPass": bool(candidate.get("hardPass")),
        "evidencePass": bool(candidate.get("evidencePass")),
        "activePass": bool(candidate.get("activePass")),
        "rejectReasons": raw_reasons,
        "selectionTier": candidate.get("selectionTier"),
        "importanceTier": candidate.get("importanceTier"),
        "importanceRank": candidate.get("importanceRank"),
        "categoryRank": category_rank,
        "disposition": _trace_disposition(candidate, selected=selected),
        "selectionReasons": _trace_selection_reasons(candidate, selected=selected),
        "render": {
            "drawingType": "horizontalLine",
            "extension": "plot",
        },
        "anchors": [
            {"role": "start", "timestamp": candidate["firstTestAt"], "price": candidate["price"]},
            {"role": "end", "timestamp": candidate["lastTestAt"], "price": candidate["price"]},
        ],
        "evidenceRefs": evidence_refs,
        "anchorPivotIds": (
            _unique_strings([ordered_evidence[0], ordered_evidence[-1]])
            if ordered_evidence else []
        ),
        "touchPivotIds": ordered_evidence,
        "reactionPivotIds": reaction_pivot_ids,
        "touchRefs": touch_refs,
        "reactionRefs": reaction_refs,
        "touches": touch_records,
        "metrics": {
            "price": candidate["price"],
            "zoneLow": candidate["zoneLow"],
            "zoneHigh": candidate["zoneHigh"],
            "touchCount": int(candidate.get("touches") or 0),
            "reactionCount": int(candidate.get("reactionCount") or 0),
            "lastTouchAgeBars": int(candidate.get("lastTouchAgeBars") or 0),
            "currentDistanceAtr": candidate.get("currentDistanceAtr", 0),
            "state": candidate.get("state"),
            "roleFlips": int(candidate.get("roleFlips") or 0),
            "vpConfluence": bool(candidate.get("vpConfluence")),
        },
    }
    if candidate.get("role") in {"support", "resistance"}:
        result["role"] = candidate["role"]
    else:
        result["kind"] = "unresolved_level"
    return result, max(0, int(candidate.get("touches") or 0) - len(touch_records))


def _trace_trend_candidate(rows, pivots, candidate, *, selected, atr, category_rank):
    pivot_by_id = {str(item["id"]): item for item in pivots}
    required = 3 if candidate["kind"] == "channel" else 2
    anchor_ids = [
        str(value) for value in candidate.get("anchorPivotIds", [])
        if str(value) in pivot_by_id
    ][:required]
    anchor_roles = ["baseStart", "baseEnd", "channelOffset"]
    anchors = [
        {
            "role": anchor_roles[index],
            "timestamp": pivot_by_id[pivot_id]["timestamp"],
            "price": round(float(pivot_by_id[pivot_id]["price"]), 6),
        }
        for index, pivot_id in enumerate(anchor_ids)
    ]
    direction = str(candidate.get("direction") or candidate["kind"])
    if candidate["kind"] == "channel" and len(anchors) == 2:
        offset_sign = 1 if direction == "up" else -1
        anchors.append({
            "role": "channelOffset",
            "timestamp": anchors[0]["timestamp"],
            "price": round(float(anchors[0]["price"]) + offset_sign * float(candidate.get("channelWidth") or 0), 6),
        })
    touch_pivot_ids = _unique_strings(candidate.get("touchPivotIds", []))
    touch_records = []
    episodes = list(candidate.get("touchEpisodes", []))
    for episode in episodes:
        index = int(episode.get("barIndex", -1))
        if not 0 <= index < len(rows):
            continue
        price_key = "low" if direction == "up" else "high"
        touch_records.append(_trace_touch(
            candidate["id"],
            timestamp=str(rows[index]["timestamp"]),
            bar_index=index,
            price=float(rows[index][price_key]),
            outcome="reaction" if episode.get("reactionPass") else "touch",
            boundary="lower" if direction == "up" else "upper",
            mfe_atr=episode.get("mfeAtr"),
            residual_atr=episode.get("residualAtr"),
        ))
    episode_bars = {int(item.get("barIndex", -1)) for item in episodes}
    for pivot_id in touch_pivot_ids:
        pivot = pivot_by_id.get(pivot_id)
        if pivot is None or int(pivot.get("barIndex", -1)) in episode_bars:
            continue
        touch_records.append(_trace_touch(
            candidate["id"],
            timestamp=str(pivot["timestamp"]),
            bar_index=int(pivot["barIndex"]),
            price=float(pivot["price"]),
            outcome="touch",
            boundary="upper" if direction == "up" else "lower",
        ))
    touch_records.sort(key=lambda item: (int(item.get("barIndex") or 0), item["id"]))
    touch_refs = [item["id"] for item in touch_records]
    reaction_refs = [
        item["id"] for item in touch_records if item.get("outcome") == "reaction"
    ]
    evidence_refs = _unique_strings([
        *anchor_ids,
        *touch_pivot_ids,
    ])
    reaction_bars = {
        int(item["barIndex"])
        for item in candidate.get("touchEpisodes", [])
        if item.get("reactionPass")
    }
    reaction_pivot_ids = [
        str(item["id"]) for item in pivots
        if int(item["barIndex"]) in reaction_bars and str(item["id"]) in touch_pivot_ids
    ]
    metrics = {
        "touchCount": int(candidate.get("touches") or 0),
        "reactionCount": int(candidate.get("reactionCount") or 0),
        "slopeAtrPerBar": round(float(candidate.get("slopeAtrPerBar") or 0), 6),
        "medianResidualAtr": round(float(candidate.get("medianResidualAtr") or 0), 6),
        "currentDistanceAtr": round(float(candidate.get("currentDistanceAtr") or 0), 6),
        "lastTouchAgeBars": int(candidate.get("lastTouchAgeBars") or 0),
        "activeInvalidation": bool(candidate.get("activeInvalidation")),
        "violationCount": int(candidate.get("violationCount") or 0),
    }
    if candidate["kind"] == "channel":
        metrics.update({
            "channelWidthAtr": round(float(candidate.get("channelWidth") or 0) / max(atr, 1e-12), 6),
            "parallelSlopeError": round(float(candidate.get("parallelSlopeError") or 0), 6),
            "containment": round(float(candidate.get("containment") or 0), 6),
        })
    result = {
        "id": candidate["id"],
        "category": "trend",
        "kind": "channel" if candidate["kind"] == "channel" else f"{candidate['kind']}trend",
        "score": candidate.get("score", 0),
        "selected": selected,
        "hardPass": bool(candidate.get("hardPass")),
        "evidencePass": bool(candidate.get("evidencePass")),
        "activePass": bool(candidate.get("activePass")),
        "rejectReasons": _trace_reject_reasons(candidate, selected=selected),
        "selectionTier": "confirmed" if selected else None,
        "importanceTier": None,
        "importanceRank": None,
        "categoryRank": category_rank,
        "disposition": _trace_disposition(candidate, selected=selected),
        "selectionReasons": _trace_selection_reasons(candidate, selected=selected),
        "direction": direction,
        "render": {
            "drawingType": "trendParallelLines" if candidate["kind"] == "channel" else "trendLine",
            "extension": "ray",
            "direction": direction,
            **({"parallelLineCount": 2} if candidate["kind"] == "channel" else {}),
        },
        "anchors": anchors,
        "evidenceRefs": evidence_refs,
        "anchorPivotIds": anchor_ids,
        "touchPivotIds": touch_pivot_ids,
        "reactionPivotIds": reaction_pivot_ids,
        "touchRefs": touch_refs,
        "reactionRefs": reaction_refs,
        "touches": touch_records,
        "metrics": metrics,
    }
    return result, max(0, int(candidate.get("touches") or 0) - len(touch_records))


def _trace_pattern_candidate(candidate, pivot_by_id, *, selected, category_rank):
    anchors = []
    geometry = candidate.get("geometry") or {}
    for boundary in ("pole", "upper", "lower"):
        segment = geometry.get(boundary)
        if not isinstance(segment, dict):
            continue
        for endpoint in ("start", "end"):
            point = segment.get(endpoint)
            if not isinstance(point, dict):
                continue
            anchors.append({
                "role": f"{boundary}{endpoint.title()}",
                "timestamp": point["timestamp"],
                "price": point["price"],
            })
    evidence_refs = _unique_strings(candidate.get("evidenceRefs", []))
    evidence_pivots = [pivot_by_id[reference] for reference in evidence_refs if reference in pivot_by_id]
    touch_pivots = evidence_pivots
    touch_pivot_ids = [str(pivot["id"]) for pivot in touch_pivots]
    touch_records = [
        _trace_touch(
            candidate["id"],
            timestamp=str(pivot["timestamp"]),
            bar_index=int(pivot["barIndex"]),
            price=float(pivot["price"]),
            outcome="touch",
            boundary="upper" if pivot.get("kind") == "H" else "lower",
        )
        for pivot in touch_pivots
    ]
    segment_indexes = [[index, index + 1] for index in range(0, len(anchors) - 1, 2)]
    confirmation = candidate.get("confirmation") or {}
    metrics = {
        "state": candidate.get("state"),
        "touchCount": int(candidate.get("touches") or 0),
        "upperTouches": int(candidate.get("upperTouches") or 0),
        "lowerTouches": int(candidate.get("lowerTouches") or 0),
        "containment": candidate.get("containment"),
        "maxResidualAtr": candidate.get("maxResidualAtr"),
        "convergenceRatio": candidate.get("convergenceRatio"),
        "parallelSlopeErrorAtr": candidate.get("parallelSlopeErrorAtr"),
        "confirmationMode": confirmation.get("mode"),
        "penetrationAtr": confirmation.get("penetrationAtr"),
        "relativeVolume": confirmation.get("relativeVolume"),
    }
    result = {
        "id": candidate["id"],
        "category": "pattern",
        "kind": candidate.get("kind"),
        "score": candidate.get("score", 0),
        "selected": selected,
        "hardPass": bool(candidate.get("hardPass")),
        "evidencePass": bool(candidate.get("evidencePass", candidate.get("hardPass"))),
        "activePass": bool(candidate.get("activePass", candidate.get("hardPass"))),
        "rejectReasons": _trace_reject_reasons(candidate, selected=selected),
        "selectionTier": "confirmed" if selected else None,
        "importanceTier": None,
        "importanceRank": None,
        "categoryRank": category_rank,
        "disposition": _trace_disposition(candidate, selected=selected),
        "selectionReasons": _trace_selection_reasons(candidate, selected=selected),
        "render": {
            "drawingType": "segments",
            "extension": "segment",
            "segments": segment_indexes,
        },
        "anchors": anchors,
        "evidenceRefs": evidence_refs,
        "anchorPivotIds": [],
        "touchPivotIds": touch_pivot_ids,
        "reactionPivotIds": [],
        "touchRefs": [item["id"] for item in touch_records],
        "reactionRefs": [],
        "touches": touch_records,
        "metrics": metrics,
    }
    return result, 0


def _trace_touch(
    candidate_id,
    *,
    timestamp,
    bar_index,
    price,
    outcome=None,
    boundary=None,
    mfe_atr=None,
    mae_atr=None,
    residual_atr=None,
):
    identity = f"{candidate_id}|{timestamp}|{bar_index}|{outcome or 'touch'}"
    result = {
        "id": f"touch-{hashlib.sha256(identity.encode()).hexdigest()[:12]}",
        "timestamp": timestamp,
        "price": round(float(price), 6),
        "barIndex": int(bar_index),
    }
    if outcome is not None:
        result["outcome"] = outcome
    if boundary is not None:
        result["boundary"] = str(boundary)
    for key, value in (("mfeAtr", mfe_atr), ("maeAtr", mae_atr), ("residualAtr", residual_atr)):
        if value is not None:
            result[key] = round(float(value), 6)
    return result


def _trace_reject_reasons(candidate, *, selected):
    reasons = _unique_strings(candidate.get("rejectReasons", []))
    return reasons


def _trace_disposition(candidate, *, selected):
    if selected:
        return "selected"
    if candidate.get("hardPass") or candidate.get("selectionTier") is not None:
        return "qualified_not_selected"
    return "rejected"


def _trace_selection_reasons(candidate, *, selected):
    if selected:
        tier = candidate.get("selectionTier")
        return [str(tier)] if tier else ["ranked_primary"]
    if candidate.get("hardPass") or candidate.get("selectionTier") is not None:
        return ["ranked_below_selection"]
    return []


def _trace_pivot(pivot):
    result = {
        "id": pivot["id"],
        "kind": pivot["kind"],
        "timestamp": pivot["timestamp"],
        "confirmedAt": pivot["confirmedAt"],
        "price": round(float(pivot["price"]), 6),
        "barIndex": int(pivot["barIndex"]),
    }
    for key in ("grade", "candleKey"):
        if pivot.get(key) is not None:
            result[key] = pivot[key]
    for key in ("strength", "reversalAtr", "prominenceAtr"):
        if pivot.get(key) is not None:
            result[key] = round(float(pivot[key]), 6)
    return result


def _unique_strings(values):
    result = []
    seen = set()
    for value in values:
        normalized = str(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


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


def _apex_bars(end, upper_slope, upper_intercept, lower_slope, lower_intercept):
    difference = upper_slope - lower_slope
    if abs(difference) < 1e-12:
        return None
    apex = (lower_intercept - upper_intercept) / difference
    return round(apex - end, 4)


def _pattern_apex_bars(geometry, index_by_timestamp, end_index):
    upper, lower = geometry.get("upper"), geometry.get("lower")
    if not upper or not lower:
        return None
    start_index = index_by_timestamp.get(str(upper["start"]["timestamp"]))
    boundary_end = index_by_timestamp.get(str(upper["end"]["timestamp"]))
    if start_index is None or boundary_end is None or boundary_end <= start_index:
        return None
    span = boundary_end - start_index
    upper_slope = (float(upper["end"]["price"]) - float(upper["start"]["price"])) / span
    lower_slope = (float(lower["end"]["price"]) - float(lower["start"]["price"])) / span
    upper_intercept = float(upper["start"]["price"]) - upper_slope * start_index
    lower_intercept = float(lower["start"]["price"]) - lower_slope * start_index
    return _apex_bars(end_index, upper_slope, upper_intercept, lower_slope, lower_intercept)


def _pattern_name(kind):
    return {
        "ascending_triangle": "상승 삼각형",
        "descending_triangle": "하락 삼각형",
        "symmetrical_triangle": "대칭 삼각형",
        "bullish_flag": "상승 깃발형",
        "bearish_flag": "하락 깃발형",
        "bullish_pennant": "상승 페넌트",
        "bearish_pennant": "하락 페넌트",
        "bullish_rectangle": "상승 직사각형",
        "bearish_rectangle": "하락 직사각형",
        "rising_wedge": "상승 쐐기",
        "falling_wedge": "하락 쐐기",
        "descending_channel_breakout": "하락 채널 상단 돌파",
        "ascending_channel_breakdown": "상승 채널 하단 이탈",
    }[kind]


def _pattern_bias(kind):
    if kind in {"descending_triangle", "bearish_flag", "bearish_pennant", "bearish_rectangle", "rising_wedge", "ascending_channel_breakdown"}:
        return "bearish"
    if kind == "symmetrical_triangle":
        return "neutral"
    return "bullish"


def _pattern_color(kind):
    bias = _pattern_bias(kind)
    return "#22c55e" if bias == "bullish" else "#ef4444" if bias == "bearish" else "#f59e0b"


def _cap(value):
    return max(0.0, min(1.0, float(value)))
