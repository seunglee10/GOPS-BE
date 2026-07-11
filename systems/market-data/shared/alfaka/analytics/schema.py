from __future__ import annotations

from typing import Any

from alfaka.serving.time_utils import canonical_utc_timestamp
from alfaka.serving.volume_profile import VOLUME_PROFILE_CALCULATION_VERSION, compute_volume_profile_payload

from .atr import atr_quality_flags, latest_atr
from .events import compute_events
from .levels import compute_levels
from .pivots import compute_pivots
from .trends import compute_regime, compute_trends


DISPLAY_BARS = {"1D": 120, "1W": 104, "1M": 36}
LOOKBACK_BARS = {"1D": 500, "1W": 312, "1M": 72}


def normalize_candles(candles: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    if interval not in DISPLAY_BARS:
        raise ValueError(f"Unsupported chart analysis interval: {interval}")
    normalized: dict[str, dict[str, Any]] = {}
    for source in candles:
        if source.get("isClosed") is False or source.get("is_closed") is False:
            continue
        timestamp = canonical_utc_timestamp(source.get("timestamp"))
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
    rows = normalize_candles(candles, interval)
    if not rows:
        return _empty_features()
    display = rows[-DISPLAY_BARS[interval]:]
    display_from = display[0]["timestamp"]
    atr = latest_atr(rows)
    profile = compute_volume_profile_payload(
        display,
        symbol="FEATURE_PACK",
        interval=interval,
        from_time=display_from,
        to_time=display[-1]["timestamp"],
        target_bins=24,
    )
    pivots = compute_pivots(rows, display_from=display_from, interval=interval)
    levels = compute_levels(
        rows,
        pivots,
        atr=atr,
        volume_profile=profile,
        expected_bars=LOOKBACK_BARS[interval],
        interval=interval,
    )
    trends = compute_trends(rows, pivots, display_from=display_from, atr=atr, interval=interval)
    regime = compute_regime(rows, trends)
    events = compute_events(rows, levels, atr=atr, display_from=display_from, interval=interval)
    return {
        "pivots": pivots,
        "levels": levels,
        "trends": trends,
        "vp": _feature_volume_profile(profile),
        "regime": regime,
        "events": events,
        "fibCandidates": _fib_candidates(pivots, rows[-1]["close"], atr),
        "qualityFlags": atr_quality_flags(rows),
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


def _fib_candidates(pivots: list[dict[str, Any]], current_price: float, atr: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for first, second in zip(pivots, pivots[1:]):
        if first["kind"] == second["kind"]:
            continue
        span = abs(float(second["price"]) - float(first["price"]))
        if span < 4 * atr:
            continue
        low, high = sorted((float(first["price"]), float(second["price"])))
        progress = (current_price - low) / span if span else 0.0
        if 0.25 <= progress <= 0.85:
            candidates.append({
                "fromPivotId": first["id"],
                "toPivotId": second["id"],
                "quality": round(min(1.0, span / max(8 * atr, 0.01)), 4),
            })
    return sorted(candidates, key=lambda item: -item["quality"])


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
        "fibCandidates": [],
        "qualityFlags": [],
    }
