from __future__ import annotations

from typing import Any

from alfaka.serving.time_utils import canonical_utc_timestamp
from alfaka.serving.volume_profile import VOLUME_PROFILE_CALCULATION_VERSION, compute_volume_profile_payload
from alfaka.storage.candle_validation import invalid_candle_numeric_reason

from .atr import abnormal_true_range_indices, latest_atr
from .config import QUALITY_CONFIG
from .events import compute_events
from .levels import compute_levels
from .patterns import compute_patterns
from .pivots import compute_pivots
from .trends import compute_regime, compute_trends


DISPLAY_BARS = {"1m": 260, "5m": 260, "10m": 260, "1h": 260, "4h": 260, "1D": 260, "1W": 192, "1M": 36}
LOOKBACK_BARS = {"1m": 380, "5m": 380, "10m": 380, "1h": 380, "4h": 380, "1D": 380, "1W": 312, "1M": 72}


def normalize_candles(candles: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    if interval not in DISPLAY_BARS:
        raise ValueError(f"Unsupported chart analysis interval: {interval}")
    normalized: dict[str, dict[str, Any]] = {}
    for source in candles:
        if source.get("isClosed") is False or source.get("is_closed") is False:
            continue
        if invalid_candle_numeric_reason(source, require=True):
            continue
        raw_timestamp = source.get("timestamp")
        trusted_analysis_row = (
            source.get("candleKey") is not None
            and source.get("barIndex") is not None
            and source.get("interval") == interval
            and isinstance(raw_timestamp, str)
            and raw_timestamp.endswith("Z")
        )
        timestamp = raw_timestamp if trusted_analysis_row else canonical_utc_timestamp(raw_timestamp)
        if not timestamp:
            continue
        try:
            row = {
                "timestamp": timestamp,
                "open": float(source["open"]),
                "high": float(source["high"]),
                "low": float(source["low"]),
                "close": float(source["close"]),
                "volume": float(source.get("volume") or 0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if row["high"] < row["low"] or min(row["open"], row["high"], row["low"], row["close"]) <= 0:
            continue
        if source.get("candleKey") is not None:
            row["candleKey"] = str(source["candleKey"])
        if source.get("barIndex") is not None:
            row["barIndex"] = int(source["barIndex"])
        row["isClosed"] = True
        row["interval"] = interval
        normalized[timestamp] = row
    result = [normalized[key] for key in sorted(normalized)][-LOOKBACK_BARS[interval]:]
    for index, row in enumerate(result):
        row.setdefault("barIndex", index)
    return result


def assemble_feature_pack(candles: list[dict[str, Any]], interval: str) -> dict[str, Any]:
    closed_sources = [
        source for source in candles
        if source.get("isClosed") is not False and source.get("is_closed") is not False
    ]
    invalid_positions = [
        index for index, source in enumerate(closed_sources)
        if invalid_candle_numeric_reason(source, require=True)
    ]
    quality_flags: list[str] = []
    source_rows = closed_sources
    if invalid_positions:
        quality_flags.append("invalid_ohlcv")
        source_rows = closed_sources[invalid_positions[-1] + 1:]
        minimum = QUALITY_CONFIG[interval].display_bars + QUALITY_CONFIG[interval].extra_anchor_bars
        if len(normalize_candles(source_rows, interval)) < minimum:
            return {**_empty_features(), "qualityFlags": [*quality_flags, "data_quality_blocked"]}
        quality_flags.append("invalid_ohlcv_trimmed")
    rows = normalize_candles(source_rows, interval)
    if not rows:
        return {**_empty_features(), "qualityFlags": quality_flags}
    analysis_rows = rows
    abnormal = abnormal_true_range_indices(rows)
    if abnormal:
        quality_flags.append("abnormal_true_range")
        clean_rows = rows[abnormal[-1] + 1:]
        minimum = QUALITY_CONFIG[interval].display_bars + QUALITY_CONFIG[interval].extra_anchor_bars
        if len(clean_rows) >= minimum:
            analysis_rows = clean_rows
            quality_flags.append("abnormal_true_range_trimmed")
        else:
            quality_flags.append("data_quality_blocked")
    if not analysis_rows:
        return {**_empty_features(), "qualityFlags": quality_flags}
    display = analysis_rows[-DISPLAY_BARS[interval]:]
    display_from = display[0]["timestamp"]
    atr = latest_atr(analysis_rows)
    profile = compute_volume_profile_payload(
        display,
        symbol="FEATURE_PACK",
        interval=interval,
        from_time=display_from,
        to_time=display[-1]["timestamp"],
        target_bins=24,
    )
    pivots = compute_pivots(analysis_rows, display_from=display_from, interval=interval)
    if "data_quality_blocked" in quality_flags:
        regime = compute_regime(analysis_rows, [])
        return {
            **_empty_features(),
            "vp": _feature_volume_profile(profile),
            "regime": regime,
            "qualityFlags": quality_flags,
        }
    levels = compute_levels(
        analysis_rows,
        pivots,
        atr=atr,
        volume_profile=profile,
        expected_bars=LOOKBACK_BARS[interval],
        interval=interval,
    )
    trends = compute_trends(analysis_rows, pivots, display_from=display_from, atr=atr, interval=interval)
    regime = compute_regime(analysis_rows, trends)
    events = compute_events(analysis_rows, levels, atr=atr, display_from=display_from, interval=interval)
    patterns = compute_patterns(analysis_rows, pivots, atr=atr, interval=interval)
    return {
        "pivots": pivots,
        "levels": levels,
        "trends": trends,
        "vp": _feature_volume_profile(profile),
        "regime": regime,
        "events": events,
        "patterns": patterns,
        "fibCandidates": _fib_candidates(pivots, analysis_rows, atr, interval),
        "qualityFlags": quality_flags,
    }


def _feature_volume_profile(payload: dict[str, Any]) -> dict[str, Any]:
    poc = payload.get("poc") or {}
    area = payload.get("valueArea") or {}
    bins = payload.get("bins") or []
    ranked = sorted(bins, key=lambda item: (-float(item.get("volume") or 0), int(item.get("index") or 0)))
    hvns = [round(float(item["priceMid"]), 2) for item in ranked[:2] if item.get("priceMid") is not None]
    lvns = [
        round(float(item["priceMid"]), 2)
        for item in sorted(bins, key=lambda item: (float(item.get("volume") or 0), int(item.get("index") or 0)))[:1]
        if item.get("priceMid") is not None
    ]
    return {
        "poc": round(float(poc["priceMid"]), 2) if poc.get("priceMid") is not None else None,
        "vah": round(float(area["high"]), 2) if area.get("high") is not None else None,
        "val": round(float(area["low"]), 2) if area.get("low") is not None else None,
        "hvns": hvns,
        "lvns": lvns,
        "binsVersion": VOLUME_PROFILE_CALCULATION_VERSION,
    }


def _fib_candidates(
    pivots: list[dict[str, Any]], candles: list[dict[str, Any]], atr: float, interval: str,
) -> list[dict[str, Any]]:
    structural = [item for item in pivots if item.get("grade") == "structural"]
    pairs = [
        (first, second, abs(float(second["price"]) - float(first["price"])))
        for first, second in zip(structural, structural[1:])
        if first["kind"] != second["kind"]
    ]
    if not pairs:
        return []
    dominant_floor = 0.75 * max(span for _first, _second, span in pairs)
    current_price = float(candles[-1]["close"])
    reaction_horizon = QUALITY_CONFIG[interval].reaction_horizon
    candidates: list[dict[str, Any]] = []
    for first, second, span in pairs:
        if span < 5 * atr or span < dominant_floor:
            continue
        low, high = sorted((float(first["price"]), float(second["price"])))
        upward = first["kind"] == "L" and second["kind"] == "H"
        retracement = (high - current_price) / span if upward else (current_price - low) / span
        if not 0.236 <= retracement <= 0.786:
            continue
        levels = [high - span * ratio if upward else low + span * ratio for ratio in (0.382, 0.5, 0.618)]
        current_distance = min(abs(current_price - price) for price in levels) / max(atr, 1e-12)
        if current_distance > 2:
            continue
        reaction = _fib_reaction(candles, second["barIndex"] + 1, levels, upward, atr, reaction_horizon)
        if reaction is None:
            continue
        candidates.append({
            "fromPivotId": first["id"],
            "toPivotId": second["id"],
            "quality": round(min(1.0, 0.55 + 0.25 * min(1, span / max(10 * atr, .01)) + 0.20 * (1 - min(1, current_distance / 2))), 4),
            "hardPass": True,
            "impulseAtr": round(span / max(atr, 1e-12), 4),
            "retracementRatio": round(retracement, 4),
            "currentDistanceAtr": round(current_distance, 4),
            "reactionAt": reaction,
        })
    return sorted(candidates, key=lambda item: -item["quality"])


def _fib_reaction(candles, start, levels, upward, atr, horizon):
    for index in range(start, len(candles)):
        row = candles[index]
        touch_price = float(row["low"] if upward else row["high"])
        if min(abs(touch_price - price) for price in levels) > .35 * atr:
            continue
        future = candles[index + 1:min(len(candles), index + 1 + horizon)]
        if upward:
            reaction = max([float(item["high"]) - touch_price for item in future] or [0])
        else:
            reaction = max([touch_price - float(item["low"]) for item in future] or [0])
        if reaction >= .75 * atr:
            return row["timestamp"]
    return None


def _empty_features() -> dict[str, Any]:
    return {
        "pivots": [],
        "levels": [],
        "trends": [],
        "vp": {"poc": None, "vah": None, "val": None, "hvns": [], "lvns": [], "binsVersion": VOLUME_PROFILE_CALCULATION_VERSION},
        "regime": {
            "trend": "range", "emaSlope20": 0.0, "atr14": 0.0, "atrPercentile": 0.0,
            "bbSqueeze": False, "bbBandwidthPercentile": 0.0, "macdState": "neutral",
            "rsi14": 50.0, "volumeZLast": 0.0, "high52w": 0.0, "low52w": 0.0,
            "pctFrom52wHigh": 0.0,
        },
        "events": [],
        "patterns": [],
        "fibCandidates": [],
        "qualityFlags": [],
    }
