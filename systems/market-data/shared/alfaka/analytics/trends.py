from __future__ import annotations

import hashlib
import math
import statistics
from typing import Any

from alfaka.serving.indicators import bollinger_bands, ema, macd, rsi

from .atr import atr_series, latest_atr, percentile_rank
from .config import QUALITY_CONFIG


def compute_trends(candles, pivots, *, display_from, atr, interval="1D"):
    config = QUALITY_CONFIG[interval]
    atr_values = [float(value or 0) for value in atr_series(candles)]
    structural = [item for item in pivots if item.get("grade") == "structural" and item["barIndex"] >= len(candles) - config.display_bars - config.extra_anchor_bars]
    lines = []
    for kind, trend_kind in (("L", "up"), ("H", "down")):
        same = [item for item in structural if item["kind"] == kind][-12:]
        hypotheses = []
        for left_index, first in enumerate(same):
            for second in same[left_index + 1:]:
                span = second["barIndex"] - first["barIndex"]
                if span <= 0: continue
                slope = (float(second["price"]) - float(first["price"])) / span
                if (trend_kind == "up" and slope <= 0) or (trend_kind == "down" and slope >= 0): continue
                inliers = [pivot for pivot in same if pivot["barIndex"] >= first["barIndex"] and abs(float(pivot["price"]) - _line(first, slope, pivot["barIndex"])) / max(atr_values[pivot["barIndex"]], 1e-12) <= 0.45]
                clusters = _touch_clusters(inliers, config.min_touch_gap)
                if len(clusters) < 3: continue
                hypotheses.append(_materialize_line(candles, first, second, slope, clusters, same, atr_values, config, interval, trend_kind))
        passed = [item for item in hypotheses if item["hardPass"]]
        if passed:
            lines.append(sorted(passed, key=lambda item: (-item["score"], item["currentDistanceAtr"], item["lastTouchAgeBars"], item["id"]))[0])
    channel = _channel(lines, pivots, candles, atr_values, interval)
    if channel:
        return [channel]
    if lines:
        return [sorted(lines, key=lambda item: (-item["score"], item["currentDistanceAtr"], item["id"]))[0]]
    range_candidate = _range_candidate(candles, structural, atr_values, config, interval)
    return [range_candidate] if range_candidate else []


def _materialize_line(candles, first, second, slope, touches, same, atr_values, config, interval, trend_kind):
    start, asof = first["barIndex"], len(candles) - 1
    residuals = [abs(float(pivot["price"]) - _line(first, slope, pivot["barIndex"])) / max(atr_values[pivot["barIndex"]], 1e-12) for pivot in touches]
    violations = 0
    for index in range(start, len(candles)):
        distance = float(candles[index]["close"]) - _line(first, slope, index)
        if (trend_kind == "up" and distance < -0.35 * atr_values[index]) or (trend_kind == "down" and distance > 0.35 * atr_values[index]): violations += 1
    current_distance = abs(float(candles[-1]["close"]) - _line(first, slope, asof)) / max(atr_values[-1], 1e-12)
    last_touch_age = asof - max(item["barIndex"] for item in touches)
    span = max(item["barIndex"] for item in touches) - min(item["barIndex"] for item in touches)
    median_atr = statistics.median([value for value in atr_values[start:] if value > 0])
    slope_atr = slope / median_atr if median_atr else 0
    hard = len(touches) >= 3 and span >= 0.25 * config.display_bars and current_distance <= 2.25 and last_touch_age <= 0.15 * config.display_bars and violations <= 1 and abs(slope_atr) >= 0.002
    score = 0.35 * min(1, (len(touches) - 2) / 3) + 0.20 * (1 - min(1, current_distance / 3)) + 0.15 * math.exp(-math.log(2) * last_touch_age / max(1, .2 * config.display_bars)) + 0.15 * (1 - min(1, statistics.median(residuals) / .35)) + 0.15 * min(1, span / (.6 * config.display_bars))
    raw = f"{interval}|{trend_kind}|{first['id']}|{second['id']}|{','.join(item['id'] for item in touches)}"
    return {
        "id": f"{interval}:trend:{hashlib.sha256(raw.encode()).hexdigest()[:10]}", "kind": trend_kind,
        "anchorPivotIds": [first["id"], second["id"]], "touchPivotIds": [item["id"] for item in touches],
        "touches": len(touches), "slopePerBar": slope, "slopeAtrPerBar": slope_atr,
        "channelWidth": None, "medianResidualAtr": statistics.median(residuals), "violationCount": violations,
        "currentDistanceAtr": current_distance, "lastTouchAgeBars": last_touch_age, "spanBars": span,
        "extension": "ray", "hardPass": hard, "score": round(max(0, min(1, score)), 4),
        "rejectReasons": [] if hard else _line_reasons(len(touches), span, current_distance, last_touch_age, violations, slope_atr, config),
    }


def _channel(lines, pivots, candles, atr_values, interval):
    if len(lines) != 2: return None
    first, second = lines
    denominator = max(abs(first["slopePerBar"]), abs(second["slopePerBar"]), 1e-12)
    if abs(first["slopePerBar"] - second["slopePerBar"]) / denominator > 0.20: return None
    pivot_map = {item["id"]: item for item in pivots}
    base = max(lines, key=lambda item: (item["score"], item["touches"], item["id"]))
    opposite = second if base is first else first
    opposite_anchor = pivot_map[opposite["touchPivotIds"][-1]]
    base_anchor = pivot_map[base["anchorPivotIds"][0]]
    width = abs(float(opposite_anchor["price"]) - _line(base_anchor, base["slopePerBar"], opposite_anchor["barIndex"]))
    median_atr = statistics.median([value for value in atr_values[min(base_anchor["barIndex"], opposite_anchor["barIndex"]):] if value > 0])
    if width <= 1.0 * median_atr: return None
    result = dict(base)
    result.update({
        "id": f"{interval}:channel:{hashlib.sha256((base['id'] + opposite['id']).encode()).hexdigest()[:10]}",
        "kind": "channel", "anchorPivotIds": [*base["anchorPivotIds"], opposite_anchor["id"]],
        "touchPivotIds": sorted(set(base["touchPivotIds"] + opposite["touchPivotIds"])),
        "touches": base["touches"] + opposite["touches"], "channelWidth": width,
        "score": round((base["score"] + opposite["score"]) / 2, 4),
    })
    return result


def _range_candidate(candles, pivots, atr_values, config, interval):
    display = candles[-config.display_bars:]
    offset = len(candles) - len(display)
    candidates = []
    for fraction in (.30, .40, .50, .60, .70, .80):
        size = max(8, int(config.display_bars * fraction))
        if len(display) < size: continue
        window, start = display[-size:], len(candles) - size
        fit_count = max(2, int(size * .8))
        fit, validation = window[:fit_count], window[fit_count:]
        local_atr = statistics.median([value for value in atr_values[start:] if value > 0])
        lower, upper = _percentile([float(item["low"]) for item in fit], .05), _percentile([float(item["high"]) for item in fit], .95)
        width = upper - lower
        if width < 2 * local_atr: continue
        lower_touches = _boundary_touches(window, lower, local_atr, "lower", config.min_touch_gap)
        upper_touches = _boundary_touches(window, upper, local_atr, "upper", config.min_touch_gap)
        if len(lower_touches) < 2 or len(upper_touches) < 2 or len(lower_touches) + len(upper_touches) < 6: continue
        if not any(index >= fit_count for index in lower_touches) or not any(index >= fit_count for index in upper_touches): continue
        sequence = sorted([(index, "L") for index in lower_touches] + [(index, "H") for index in upper_touches])
        alternations = sum(left[1] != right[1] for left, right in zip(sequence, sequence[1:]))
        if alternations < 3: continue
        containment = sum(lower - .25 * local_atr <= float(item["close"]) <= upper + .25 * local_atr for item in validation) / max(1, len(validation))
        travel = abs(float(window[-1]["close"]) - float(window[0]["close"]))
        path = sum(abs(float(right["close"]) - float(left["close"])) for left, right in zip(window, window[1:]))
        efficiency, net = travel / path if path else 0, travel / width
        swings = [item for item in pivots if start <= item["barIndex"] < len(candles)]
        ordered = sum((right["price"] > left["price"]) == (float(window[-1]["close"]) > float(window[0]["close"])) for left, right in zip(swings, swings[1:]))
        ordered_ratio = ordered / max(1, len(swings) - 1)
        if efficiency >= .45 and net >= .70 and ordered_ratio >= .70: continue
        if containment < .80 or _zone_distance(float(candles[-1]["close"]), lower, upper) / local_atr > 1: continue
        raw = f"{interval}|range|{window[0].get('candleKey')}|{window[-1].get('candleKey')}|{lower}|{upper}"
        candidates.append({
            "id": f"{interval}:range:{hashlib.sha256(raw.encode()).hexdigest()[:10]}", "kind": "range",
            "anchorPivotIds": [], "touches": len(lower_touches) + len(upper_touches), "slopePerBar": 0,
            "channelWidth": width, "rangeFrom": window[0]["timestamp"], "rangeTo": window[-1]["timestamp"],
            "rangeHigh": upper, "rangeLow": lower, "hardPass": True, "score": round(containment, 4),
            "currentDistanceAtr": _zone_distance(float(candles[-1]["close"]), lower, upper) / local_atr,
            "lastTouchAgeBars": size - 1 - max(lower_touches + upper_touches), "spanBars": size,
            "lowerTouchBars": lower_touches, "upperTouchBars": upper_touches, "rejectReasons": [], "extension": "segment",
        })
    return sorted(candidates, key=lambda item: (-item["score"], -item["touches"], item["spanBars"], item["id"]))[0] if candidates else None


def compute_regime(candles, trends):
    closes = [float(item["close"]) for item in candles]; highs = [float(item["high"]) for item in candles]; lows = [float(item["low"]) for item in candles]; volumes = [float(item.get("volume") or 0) for item in candles]
    atr_values = atr_series(candles); atr14 = latest_atr(candles); ema20 = ema(closes, 20); valid_ema = [value for value in ema20 if value is not None]
    ema_slope = ((valid_ema[-1] - valid_ema[-6]) / 5 / atr14) if len(valid_ema) >= 6 else 0.0
    bands = bollinger_bands(closes, 20, 2.0); bandwidths = [((upper-lower)/middle) if middle not in {None,0} and upper is not None and lower is not None else None for middle,upper,lower in bands]
    current = next((value for value in reversed(bandwidths) if value is not None), None); percentile = percentile_rank(bandwidths, current)
    macd_values = macd(closes, 12, 26, 9); rsi_values = rsi(closes, 14); rsi14 = next((value for value in reversed(rsi_values) if value is not None), 50.0)
    lookback = candles[-min(252,len(candles)):]; high52=max(float(item["high"]) for item in lookback); low52=min(float(item["low"]) for item in lookback); last=closes[-1]
    trend = next((item["kind"] for item in trends if item["kind"] in {"up","down"}), "range")
    if trend == "range": trend = "up" if ema_slope > .12 else "down" if ema_slope < -.12 else "range"
    return {"trend":trend,"emaSlope20":round(ema_slope,4),"atr14":round(atr14,2),"atrPercentile":round(percentile_rank(atr_values,atr14),4),"bbSqueeze":percentile<.2,"bbBandwidthPercentile":round(percentile,4),"macdState":_macd_state(macd_values,closes),"rsi14":round(float(rsi14),2),"volumeZLast":round(_zscore_last(volumes),2),"high52w":round(high52,2),"low52w":round(low52,2),"pctFrom52wHigh":round(((last/high52)-1)*100,2) if high52 else 0}


def _line(first, slope, index): return float(first["price"]) + slope * (index - first["barIndex"])
def _touch_clusters(items, gap):
    result=[]
    for item in sorted(items,key=lambda value:value["barIndex"]):
        if not result or item["barIndex"]-result[-1]["barIndex"]>=gap: result.append(item)
        elif float(item.get("strength") or 0)>float(result[-1].get("strength") or 0): result[-1]=item
    return result
def _line_reasons(touches,span,distance,age,violations,slope,config):
    result=[]
    if touches<3: result.append("two_point_only")
    if span<.25*config.display_bars: result.append("short_span")
    if distance>2.25: result.append("no_current_relevance")
    if age>.15*config.display_bars: result.append("stale")
    if violations>1: result.append("close_violation")
    if abs(slope)<.002: result.append("flat_slope")
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
