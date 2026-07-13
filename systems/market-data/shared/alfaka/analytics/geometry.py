from __future__ import annotations

import hashlib
import statistics
from typing import Any

from alfaka.serving.volume_profile import compute_volume_profile_payload

from .atr import latest_atr as regression_atr
from .levels import compute_levels
from .patterns import TRIANGLE_KINDS, compute_patterns, compute_triangles
from .pivots import compute_pivots
from .trade_timing import evaluate_pattern_trade_timing


SUPPORTED_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")
TARGET_BARS = {**{interval: 380 for interval in SUPPORTED_INTERVALS[:-1]}, "1W": 312}
WARMUP_BARS = {interval: 120 for interval in SUPPORTED_INTERVALS}
EVALUATION_BARS = {**{interval: 260 for interval in SUPPORTED_INTERVALS[:-1]}, "1W": 192}
MINIMUM_BARS = 120
ALGORITHM_VERSION = "ohlcv-consensus-pattern-families-v3"

_ATR_PERIOD = 14
_VOLUME_BASELINE = 20


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
    supports, resistances = _confirmed_horizontal_levels(
        symbol, interval, rows, current=current, atr=latest_atr,
    )
    pattern_candidates = _regression_pattern_candidates(rows, interval=interval)
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
        *(_pattern_drawings(symbol, interval, primary_pattern, generated_at) if primary_pattern else []),
    ][:8]
    return {
        "algorithmVersion": ALGORITHM_VERSION,
        "supports": supports,
        "resistances": resistances,
        "patterns": [_public_pattern(item) for item in active_patterns[:8]],
        "primaryPattern": public_primary_pattern,
        "tradePlan": trade_plan,
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


def _confirmed_horizontal_levels(symbol, interval, rows, *, current, atr):
    """Return only evidence-confirmed levels that are still relevant now."""
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
    )
    evidence_by_id = {str(item["id"]): item for item in pivots}
    levels = []
    for candidate in candidates:
        if not candidate["hardPass"]:
            continue
        role = candidate["role"]
        role_side_pass = (
            role == "support" and current >= float(candidate["zoneLow"]) - 0.25 * atr
        ) or (
            role == "resistance" and current <= float(candidate["zoneHigh"]) + 0.25 * atr
        )
        if not role_side_pass:
            continue
        price = float(candidate["price"])
        levels.append({
            "id": candidate["id"],
            "role": role,
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
        })
    ordered = sorted(
        levels,
        key=lambda item: (-item["score"], item["currentDistanceAtr"], item["id"]),
    )
    supports = [item for item in ordered if item["role"] == "support"][:2]
    resistances = [item for item in ordered if item["role"] == "resistance"][:2]
    return supports, resistances


def _regression_triangle_candidates(
    rows: list[dict[str, Any]], *, interval: str,
) -> list[dict[str, Any]]:
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
    rows: list[dict[str, Any]], *, interval: str,
) -> list[dict[str, Any]]:
    display_from = rows[max(0, len(rows) - EVALUATION_BARS[interval])]["timestamp"]
    pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    candidates = compute_patterns(rows, pivots, atr=regression_atr(rows), interval=interval)
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


def _level_drawing(symbol, interval, level, generated_at):
    color = "#22c55e" if level["role"] == "support" else "#ef4444"
    return _drawing(symbol, interval, level["id"], "horizontalLine", level["anchors"], color, "지지" if level["role"] == "support" else "저항", generated_at, opacity=0.86)


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
        "apexBarsFromAsOf", "evidence",
    )}


def _public_pattern(value):
    if value is None:
        return None
    keys = (
        "id", "kind", "state", "breakoutDirection", "score", "touches", "upperTouches", "lowerTouches",
        "containment", "convergenceRatio", "parallelSlopeErrorAtr", "maxResidualAtr", "poleAtr",
        "poleEfficiency", "retracementRatio", "channelWidthAtr", "geometryHash", "pole", "upper", "lower",
        "apexBarsFromAsOf", "evidence",
    )
    return {
        **{key: value[key] for key in keys if key in value},
        "bias": _pattern_bias(value["kind"]),
    }


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
