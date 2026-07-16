from __future__ import annotations

import hashlib
import math
import statistics
from typing import Any

from alfaka.serving.indicators import bollinger_bands, ema, macd, rsi

from .atr import atr_series, latest_atr, percentile_rank
from .config import QUALITY_CONFIG
from .pivots import compute_pivots


def compute_trends(
    candles,
    pivots=None,
    *,
    display_from,
    atr,
    interval="1D",
    retain_competitors=False,
):
    config = QUALITY_CONFIG[interval]
    if pivots is None:
        pivots = compute_pivots(candles, display_from=display_from, interval=interval)
    atr_values = [float(value or 0) for value in atr_series(candles)]
    structural = [item for item in pivots if item.get("grade") == "structural" and item["barIndex"] >= len(candles) - config.display_bars - config.extra_anchor_bars]
    hypotheses = []
    for kind, trend_kind in (("L", "up"), ("H", "down")):
        same = [item for item in structural if item["kind"] == kind][-12:]
        for left_index, first in enumerate(same):
            for second in same[left_index + 1:]:
                span = second["barIndex"] - first["barIndex"]
                if span <= 0: continue
                slope = (float(second["price"]) - float(first["price"])) / span
                if (trend_kind == "up" and slope <= 0) or (trend_kind == "down" and slope >= 0): continue
                touches = _line_touch_episodes(candles, first, slope, trend_kind, atr_values, config)
                hypotheses.append(_materialize_line(candles, first, second, slope, touches, atr_values, config, interval, trend_kind, same))
    hypotheses = sorted(
        hypotheses,
        key=lambda item: (-int(item["hardPass"]), -item["score"], item["currentDistanceAtr"], item["id"]),
    )
    passed_lines = []
    for kind in ("up", "down"):
        candidates = [item for item in hypotheses if item["kind"] == kind and item["hardPass"]]
        passed_lines.extend(candidates[:3])
    channels = [
        candidate for base in passed_lines
        if (candidate := _channel(base, structural, candles, atr_values, interval)) is not None
    ]
    passed_channels = [item for item in channels if item["hardPass"]]
    trend_primary: list[dict[str, Any]] = []
    if passed_channels:
        trend_primary = [sorted(passed_channels, key=lambda item: (-item["score"], item["id"]))[0]]
    elif passed_lines:
        trend_primary = [sorted(passed_lines, key=lambda item: (-item["score"], item["currentDistanceAtr"], item["id"]))[0]]

    # A current range can form after a valid older trend. Evaluate both under
    # their full confirmed gates, then keep the more actionable structure;
    # skipping range detection whenever any line passed caused real bases to
    # disappear behind long-lived trend evidence.
    range_candidates = _range_candidates(candles, structural, atr_values, config, interval)
    passed_ranges = [item for item in range_candidates if item["hardPass"]]
    range_primary = (
        [sorted(passed_ranges, key=lambda item: (-item["score"], -item["touches"], item["id"]))[0]]
        if passed_ranges else []
    )
    selected = sorted(
        [*trend_primary, *range_primary],
        key=lambda item: (
            -float(item.get("score") or 0),
            float(item["currentDistanceAtr"]) if item.get("currentDistanceAtr") is not None else 99.0,
            int(item["lastTouchAgeBars"]) if item.get("lastTouchAgeBars") is not None else 999,
            item["id"],
        ),
    )[:2]

    selected_ids = {item["id"] for item in selected}
    rejected = [
        item for item in (*hypotheses, *channels, *range_candidates)
        if not item["hardPass"] and item["id"] not in selected_ids
    ]
    rejected = sorted(
        rejected,
        key=lambda item: (-item["score"], item.get("currentDistanceAtr", 99), item["id"]),
    )
    if not retain_competitors:
        return [*selected, *rejected[:8]]
    competitors = sorted(
        (
            item for item in (*hypotheses, *channels, *range_candidates)
            if item["hardPass"] and item["id"] not in selected_ids
        ),
        key=lambda item: (
            0 if item["kind"] == "channel" else 1 if item["kind"] in {"up", "down"} else 2,
            -float(item.get("score") or 0),
            float(item["currentDistanceAtr"]) if item.get("currentDistanceAtr") is not None else 99.0,
            str(item["id"]),
        ),
    )
    return [*selected, *competitors, *rejected]


def _materialize_line(candles, first, second, slope, touches, atr_values, config, interval, trend_kind, same):
    start, asof = first["barIndex"], len(candles) - 1
    residuals = [float(item["residualAtr"]) for item in touches]
    reactions = [item for item in touches if item["reactionPass"]]
    invalidations = _invalidation_episodes(candles, first, slope, trend_kind, atr_values, start)
    last_reaction = max((item["barIndex"] for item in reactions), default=-1)
    active_invalidations = [item for item in invalidations if item > last_reaction]
    last_cleared = max((item for item in invalidations if item <= last_reaction), default=start - 1)
    last_touch = max((item["barIndex"] for item in touches), default=start)
    adverse_start = max(start, last_cleared + 1)
    adverse = 0
    for index in range(adverse_start, last_touch + 1):
        local_atr = max(atr_values[index], 1e-12)
        signed = (float(candles[index]["close"]) - _line(first, slope, index)) / local_atr
        if (trend_kind == "up" and signed < -.35) or (trend_kind == "down" and signed > .35):
            adverse += 1
    adverse_ratio = adverse / max(1, last_touch - adverse_start + 1)
    current_distance = abs(float(candles[-1]["close"]) - _line(first, slope, asof)) / max(atr_values[-1], 1e-12)
    last_touch_age = asof - last_touch
    span = max((item["barIndex"] for item in touches), default=start) - min((item["barIndex"] for item in touches), default=start)
    median_atr = statistics.median([value for value in atr_values[start:] if value > 0])
    slope_atr = slope / median_atr if median_atr else 0
    median_residual = statistics.median(residuals) if residuals else 99.0
    evidence_pass = len(touches) >= 3 and len(reactions) >= 2 and span >= .25 * config.display_bars and median_residual <= .35
    active_pass = (
        current_distance <= 2.25
        and last_touch_age <= .20 * config.display_bars
        and not active_invalidations
        and adverse_ratio <= .05
        and abs(slope_atr) >= .002
    )
    hard = evidence_pass and active_pass
    score = 0.25 * min(1, (len(touches) - 2) / 3) + 0.15 * min(1, len(reactions) / 3) + 0.15 * (1 - min(1, current_distance / 3)) + 0.15 * math.exp(-math.log(2) * last_touch_age / max(1, .2 * config.display_bars)) + 0.15 * (1 - min(1, median_residual / .35)) + 0.15 * min(1, span / (.6 * config.display_bars))
    pivot_by_bar = {item["barIndex"]: item for item in same}
    touch_pivots = [pivot_by_bar[item["barIndex"]]["id"] for item in touches if item["barIndex"] in pivot_by_bar]
    raw = f"{interval}|{trend_kind}|{first['id']}|{second['id']}|{','.join(item['candleKey'] for item in touches)}"
    return {
        "id": f"{interval}:trend:{hashlib.sha256(raw.encode()).hexdigest()[:10]}", "kind": trend_kind,
        "anchorPivotIds": [first["id"], second["id"]], "touchPivotIds": touch_pivots,
        "touchCandleKeys": [item["candleKey"] for item in touches], "touchEpisodes": touches,
        "touches": len(touches), "slopePerBar": slope, "slopeAtrPerBar": slope_atr,
        "reactionCount": len(reactions), "channelWidth": None, "medianResidualAtr": median_residual,
        "violationCount": len(active_invalidations), "invalidationEpisodes": invalidations[-8:],
        "activeInvalidation": bool(active_invalidations), "adverseCloseRatio": round(adverse_ratio, 4),
        "currentDistanceAtr": current_distance, "lastTouchAgeBars": last_touch_age, "spanBars": span,
        "extension": "ray", "evidencePass": evidence_pass, "activePass": active_pass,
        "hardPass": hard, "score": round(max(0, min(1, score)), 4),
        "rejectReasons": [] if hard else _line_reasons(len(touches), len(reactions), span, median_residual, current_distance, last_touch_age, bool(active_invalidations), adverse_ratio, slope_atr, config),
    }


def _channel(base, pivots, candles, atr_values, interval):
    pivot_map = {item["id"]: item for item in pivots}
    base_anchor = pivot_map.get(base["anchorPivotIds"][0])
    if base_anchor is None:
        return None
    opposite_kind = "H" if base["kind"] == "up" else "L"
    opposite = [item for item in pivots if item["kind"] == opposite_kind and item["barIndex"] >= base_anchor["barIndex"]]
    if len(opposite) < 2:
        return None
    fit_slope = _regression_slope(opposite)
    denominator = max(abs(base["slopePerBar"]), abs(fit_slope), 1e-12)
    parallel_error = abs(base["slopePerBar"] - fit_slope) / denominator
    signed_offsets = [float(item["price"]) - _line(base_anchor, base["slopePerBar"], item["barIndex"]) for item in opposite]
    expected_sign = 1 if base["kind"] == "up" else -1
    usable = [(item, offset) for item, offset in zip(opposite, signed_offsets) if offset * expected_sign > 0]
    if len(usable) < 2:
        return None
    width = statistics.median(abs(offset) for _item, offset in usable)
    median_atr = statistics.median([value for value in atr_values[base_anchor["barIndex"]:] if value > 0])
    boundary = [
        item for item, offset in usable
        if abs(abs(offset) - width) <= .45 * max(atr_values[item["barIndex"]], 1e-12)
    ]
    boundary = _touch_clusters(boundary, QUALITY_CONFIG[interval].min_touch_gap)
    third = min(boundary, key=lambda item: abs(abs(float(item["price"]) - _line(base_anchor, base["slopePerBar"], item["barIndex"])) - width)) if boundary else None
    closes = candles[base_anchor["barIndex"]:]
    contained = 0
    for index, row in enumerate(closes, start=base_anchor["barIndex"]):
        baseline = _line(base_anchor, base["slopePerBar"], index)
        lower, upper = sorted((baseline, baseline + expected_sign * width))
        tolerance = .25 * max(atr_values[index], 1e-12)
        contained += lower - tolerance <= float(row["close"]) <= upper + tolerance
    containment = contained / max(1, len(closes))
    evidence_pass = base["hardPass"] and len(boundary) >= 2
    active_pass = parallel_error <= .20 and width >= median_atr and containment >= .80
    hard = evidence_pass and active_pass and third is not None
    raw = f"{interval}|channel|{base['id']}|{','.join(item['id'] for item in boundary)}"
    result = dict(base)
    result.update({
        "id": f"{interval}:channel:{hashlib.sha256(raw.encode()).hexdigest()[:10]}",
        "kind": "channel",
        "direction": base["kind"],
        "anchorPivotIds": [*base["anchorPivotIds"], *([third["id"]] if third else [])],
        "touchPivotIds": sorted(set(base["touchPivotIds"] + [item["id"] for item in boundary])),
        "touches": base["touches"] + len(boundary), "oppositeTouches": len(boundary),
        "channelWidth": width, "parallelSlopeError": round(parallel_error, 4),
        "containment": round(containment, 4), "evidencePass": evidence_pass,
        "activePass": active_pass, "hardPass": hard,
        "score": round(max(0, min(1, .55 * base["score"] + .20 * min(1, len(boundary) / 3) + .25 * containment)), 4),
        "rejectReasons": [] if hard else _channel_reasons(len(boundary), parallel_error, width, median_atr, containment),
    })
    return result


def _range_candidates(candles, pivots, atr_values, config, interval):
    display = candles[-config.display_bars:]
    offset = len(candles) - len(display)
    candidates = []
    for fraction in (.30, .40, .50, .60, .70, .80):
        size = max(8, int(config.display_bars * fraction))
        if len(display) < size: continue
        window, start = display[-size:], len(candles) - size
        fit_count = max(2, int(size * .8))
        fit, validation = window[:fit_count], window[fit_count:]
        local_atrs = [value for value in atr_values[start:] if value > 0]
        if not local_atrs:
            continue
        local_atr = statistics.median(local_atrs)
        lower, upper = _percentile([float(item["low"]) for item in fit], .05), _percentile([float(item["high"]) for item in fit], .95)
        width = upper - lower
        lower_touches = _boundary_touches(window, lower, local_atr, "lower", config.min_touch_gap)
        upper_touches = _boundary_touches(window, upper, local_atr, "upper", config.min_touch_gap)
        lower_touches, upper_touches, ambiguous_touches = _independent_boundary_touches(
            lower_touches,
            upper_touches,
        )
        sequence = sorted([(index, "L") for index in lower_touches] + [(index, "H") for index in upper_touches])
        alternations = sum(left[1] != right[1] for left, right in zip(sequence, sequence[1:]))
        containment = sum(lower - .25 * local_atr <= float(item["close"]) <= upper + .25 * local_atr for item in validation) / max(1, len(validation))
        travel = abs(float(window[-1]["close"]) - float(window[0]["close"]))
        path = sum(abs(float(right["close"]) - float(left["close"])) for left, right in zip(window, window[1:]))
        efficiency, net = travel / path if path else 0, travel / max(width, 1e-12)
        swings = [item for item in pivots if start <= item["barIndex"] < len(candles)]
        ordered = sum((right["price"] > left["price"]) == (float(window[-1]["close"]) > float(window[0]["close"])) for left, right in zip(swings, swings[1:]))
        ordered_ratio = ordered / max(1, len(swings) - 1)
        directional_reject = efficiency >= .45 and net >= .70 and ordered_ratio >= .70
        current_distance = _zone_distance(float(candles[-1]["close"]), lower, upper) / local_atr
        recent_half = int(size * .50)
        recent_tail = int(size * .80)
        both_recent = any(index >= recent_half for index in lower_touches) and any(index >= recent_half for index in upper_touches)
        one_in_tail = any(index >= recent_tail for index in (*lower_touches, *upper_touches))
        evidence_pass = (
            width >= 2 * local_atr
            and len(lower_touches) >= 2
            and len(upper_touches) >= 2
            and len(lower_touches) + len(upper_touches) >= 5
            and alternations >= 3
            and both_recent
            and one_in_tail
        )
        active_pass = containment >= .85 and current_distance <= .75 and not directional_reject
        hard = evidence_pass and active_pass
        touch_count = len(lower_touches) + len(upper_touches)
        last_touch_age = size - 1 - max(lower_touches + upper_touches, default=0)
        # A raw containment ratio is not comparable with the composite line and
        # channel scores: a minimally confirmed range commonly has 1.0
        # containment and would therefore hide a much stronger current trend.
        # Rank ranges on the same evidence/current-actionability dimensions
        # used by the confirmed contract without relaxing any hard gate.
        touch_score = min(1.0, max(0.0, (touch_count - 4) / 4))
        alternation_score = min(1.0, max(0.0, (alternations - 2) / 3))
        containment_score = min(1.0, max(0.0, (containment - .85) / .15))
        recency_score = 1 - min(1.0, last_touch_age / max(1, .20 * config.display_bars))
        distance_score = 1 - min(1.0, current_distance / .75)
        score = (
            .30 * touch_score
            + .20 * alternation_score
            + .20 * containment_score
            + .15 * recency_score
            + .15 * distance_score
        )
        raw = f"{interval}|range|{window[0].get('candleKey')}|{window[-1].get('candleKey')}|{lower}|{upper}"
        candidates.append({
            "id": f"{interval}:range:{hashlib.sha256(raw.encode()).hexdigest()[:10]}", "kind": "range",
            "anchorPivotIds": [], "touches": touch_count, "slopePerBar": 0,
            "channelWidth": width, "rangeFrom": window[0]["timestamp"], "rangeTo": window[-1]["timestamp"],
            "rangeHigh": upper, "rangeLow": lower, "evidencePass": evidence_pass,
            "activePass": active_pass, "hardPass": hard, "score": round(score, 4),
            "currentDistanceAtr": current_distance,
            "lastTouchAgeBars": last_touch_age, "spanBars": size,
            "lowerTouchBars": lower_touches, "upperTouchBars": upper_touches,
            "ambiguousBoundaryTouchBars": ambiguous_touches,
            "alternations": alternations, "containment": round(containment, 4),
            "bothBoundariesRecent": both_recent, "tailBoundaryConfirmed": one_in_tail,
            "directionalEfficiencyRejected": directional_reject,
            "rejectReasons": [] if hard else _range_reasons(width, local_atr, lower_touches, upper_touches, alternations, both_recent, one_in_tail, containment, current_distance, directional_reject),
            "extension": "segment",
        })
    return sorted(candidates, key=lambda item: (-int(item["hardPass"]), -item["score"], -item["touches"], item["spanBars"], item["id"]))


def compute_regime(candles, trends):
    closes = [float(item["close"]) for item in candles]; highs = [float(item["high"]) for item in candles]; lows = [float(item["low"]) for item in candles]; volumes = [float(item.get("volume") or 0) for item in candles]
    atr_values = atr_series(candles); atr14 = latest_atr(candles); ema20 = ema(closes, 20); valid_ema = [value for value in ema20 if value is not None]
    ema_slope = ((valid_ema[-1] - valid_ema[-6]) / 5 / atr14) if len(valid_ema) >= 6 else 0.0
    bands = bollinger_bands(closes, 20, 2.0); bandwidths = [((upper-lower)/middle) if middle not in {None,0} and upper is not None and lower is not None else None for middle,upper,lower in bands]
    current = next((value for value in reversed(bandwidths) if value is not None), None); percentile = percentile_rank(bandwidths, current)
    macd_values = macd(closes, 12, 26, 9); rsi_values = rsi(closes, 14); rsi14 = next((value for value in reversed(rsi_values) if value is not None), 50.0)
    lookback = candles[-min(252,len(candles)):]; high52=max(float(item["high"]) for item in lookback); low52=min(float(item["low"]) for item in lookback); last=closes[-1]
    trend = next((
        item.get("direction") or item["kind"]
        for item in trends
        if item.get("hardPass") and (item["kind"] in {"up","down"} or item.get("direction") in {"up","down"})
    ), "range")
    if trend == "range": trend = "up" if ema_slope > .12 else "down" if ema_slope < -.12 else "range"
    return {"trend":trend,"emaSlope20":round(ema_slope,4),"atr14":round(atr14,2),"atrPercentile":round(percentile_rank(atr_values,atr14),4),"bbSqueeze":percentile<.2,"bbBandwidthPercentile":round(percentile,4),"macdState":_macd_state(macd_values,closes),"rsi14":round(float(rsi14),2),"volumeZLast":round(_zscore_last(volumes),2),"high52w":round(high52,2),"low52w":round(low52,2),"pctFrom52wHigh":round(((last/high52)-1)*100,2) if high52 else 0}


def _line(first, slope, index): return float(first["price"]) + slope * (index - first["barIndex"])


def _line_touch_episodes(candles, first, slope, trend_kind, atr_values, config):
    candidates = []
    for index in range(first["barIndex"], len(candles)):
        row = candles[index]
        local_atr = max(atr_values[index], 1e-12)
        touch_price = float(row["low"] if trend_kind == "up" else row["high"])
        residual = abs(touch_price - _line(first, slope, index)) / local_atr
        if residual > .45:
            continue
        future = candles[index + 1:min(len(candles), index + 1 + config.reaction_horizon)]
        if trend_kind == "up":
            mfe = max([float(item["high"]) - _line(first, slope, future_index) for future_index, item in enumerate(future, start=index + 1)] or [0]) / local_atr
        else:
            mfe = max([_line(first, slope, future_index) - float(item["low"]) for future_index, item in enumerate(future, start=index + 1)] or [0]) / local_atr
        candidates.append({
            "barIndex": index,
            "timestamp": row["timestamp"],
            "candleKey": str(row.get("candleKey") or row["timestamp"]),
            "residualAtr": round(residual, 4),
            "mfeAtr": round(max(0, mfe), 4),
            "reactionPass": mfe >= .75,
        })
    result = []
    for item in candidates:
        if not result or item["barIndex"] - result[-1]["barIndex"] >= config.min_touch_gap:
            result.append(item)
        elif (item["residualAtr"], -item["mfeAtr"], item["barIndex"]) < (result[-1]["residualAtr"], -result[-1]["mfeAtr"], result[-1]["barIndex"]):
            result[-1] = item
    return result


def _invalidation_episodes(candles, first, slope, trend_kind, atr_values, start):
    result = []
    consecutive = 0
    for index in range(start, len(candles)):
        local_atr = max(atr_values[index], 1e-12)
        signed = (float(candles[index]["close"]) - _line(first, slope, index)) / local_atr
        adverse = -signed if trend_kind == "up" else signed
        if adverse > 1:
            result.append(index)
            consecutive = 0
        elif adverse > .5:
            consecutive += 1
            if consecutive == 2:
                result.append(index)
                consecutive = 0
        else:
            consecutive = 0
    return result


def _touch_clusters(items, gap):
    result=[]
    for item in sorted(items,key=lambda value:value["barIndex"]):
        if not result or item["barIndex"]-result[-1]["barIndex"]>=gap: result.append(item)
        elif float(item.get("strength") or 0)>float(result[-1].get("strength") or 0): result[-1]=item
    return result
def _line_reasons(touches,reactions,span,residual,distance,age,active_invalidation,adverse_ratio,slope,config):
    result=[]
    if touches<3: result.append("two_point_only")
    if reactions<2: result.append("insufficient_reaction_episodes")
    if span<.25*config.display_bars: result.append("short_span")
    if residual>.35: result.append("high_residual")
    if distance>2.25: result.append("no_current_relevance")
    if age>.20*config.display_bars: result.append("stale")
    if active_invalidation: result.append("active_invalidation")
    if adverse_ratio>.05: result.append("adverse_close_ratio")
    if abs(slope)<.002: result.append("flat_slope")
    return result


def _regression_slope(items):
    xs = [float(item["barIndex"]) for item in items]
    ys = [float(item["price"]) for item in items]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else 0.0


def _channel_reasons(touches, parallel_error, width, atr, containment):
    result = []
    if touches < 2: result.append("insufficient_opposite_touches")
    if parallel_error > .20: result.append("parallel_error")
    if width < atr: result.append("narrow_channel")
    if containment < .80: result.append("low_containment")
    return result


def _range_reasons(width, atr, lower, upper, alternations, both_recent, tail_recent, containment, distance, directional):
    result = []
    if width < 2 * atr: result.append("narrow_range")
    if len(lower) < 2 or len(upper) < 2: result.append("insufficient_boundary_touches")
    if len(lower) + len(upper) < 5: result.append("insufficient_touch_episodes")
    if alternations < 3: result.append("insufficient_alternations")
    if not both_recent: result.append("stale_boundary")
    if not tail_recent: result.append("no_recent_boundary")
    if containment < .85: result.append("low_containment")
    if distance > .75: result.append("no_current_relevance")
    if directional: result.append("directional_efficiency")
    return result
def _percentile(values,q):
    ordered=sorted(values); position=(len(ordered)-1)*q; lower=math.floor(position); upper=math.ceil(position)
    return ordered[lower] if lower==upper else ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)
def _boundary_touches(rows,boundary,atr,side,gap):
    result=[]
    for index,row in enumerate(rows):
        value=float(row["low"] if side=="lower" else row["high"])
        if abs(value-boundary)<=.45*atr and (not result or index-result[-1]>=gap): result.append(index)
    return result
def _independent_boundary_touches(lower_touches,upper_touches):
    """A single wide candle is not two independent boundary episodes."""
    ambiguous=sorted(set(lower_touches).intersection(upper_touches))
    if not ambiguous:return list(lower_touches),list(upper_touches),[]
    ambiguous_set=set(ambiguous)
    return ([index for index in lower_touches if index not in ambiguous_set],[index for index in upper_touches if index not in ambiguous_set],ambiguous)
def _zone_distance(price,low,high): return low-price if price<low else price-high if price>high else 0
def _macd_state(values,closes):
    for previous,current in zip(values[-6:],values[-5:]):
        if None in previous[:2] or None in current[:2]: continue
        if previous[0]<=previous[1] and current[0]>current[1]: return "bullish_cross_recent"
        if previous[0]>=previous[1] and current[0]<current[1]: return "bearish_cross_recent"
    return "neutral"
def _zscore_last(values):
    window=values[-20:]
    if len(window)<2:return 0
    deviation=statistics.pstdev(window)
    return (window[-1]-statistics.mean(window))/deviation if deviation else 0
