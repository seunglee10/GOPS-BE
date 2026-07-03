from __future__ import annotations

from statistics import mean
from typing import Any

ANALYSIS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "supportResistance",
        "description": "Find repeated local high and low price areas that can be used as support or resistance candidates.",
        "outputs": ["supports", "resistances"],
    },
    {
        "name": "swingPoints",
        "description": "Find recent high and low swing points from the provided candles.",
        "outputs": ["low", "high"],
    },
    {
        "name": "trendCandidates",
        "description": "Build candidate trend lines from recent swing points.",
        "outputs": ["uptrend", "downtrend"],
    },
    {
        "name": "volumeEvents",
        "description": "Find unusually high-volume candles and their relative volume strength.",
        "outputs": ["events"],
    },
    {
        "name": "maContext",
        "description": "Summarize the latest close relative to MA5, MA20, and MA60.",
        "outputs": ["latestClose", "movingAverages", "bias"],
    },
]


def analysis_tools() -> list[dict[str, Any]]:
    return ANALYSIS_TOOL_DEFINITIONS


def build_analysis_snapshot(candle_sets: dict[str, list[dict[str, Any]]], requested_tools: list[str] | None = None) -> dict[str, Any]:
    tools = set(requested_tools or [tool["name"] for tool in ANALYSIS_TOOL_DEFINITIONS])
    snapshot: dict[str, Any] = {}
    for key, candles in candle_sets.items():
        if not candles:
            snapshot[key] = {"count": 0}
            continue
        result: dict[str, Any] = {"count": len(candles)}
        if "supportResistance" in tools:
            result["supportResistance"] = support_resistance(candles)
        if "swingPoints" in tools:
            result["swingPoints"] = swing_points(candles)
        if "trendCandidates" in tools:
            result["trendCandidates"] = trend_candidates(candles)
        if "volumeEvents" in tools:
            result["volumeEvents"] = volume_events(candles)
        if "maContext" in tools:
            result["maContext"] = ma_context(candles)
        snapshot[key] = result
    return snapshot


def support_resistance(candles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    window = candles[-min(len(candles), 160):]
    if len(window) < 5:
        return {"supports": [], "resistances": []}
    pivot_highs: list[dict[str, Any]] = []
    pivot_lows: list[dict[str, Any]] = []
    for index in range(2, len(window) - 2):
        candle = window[index]
        high = safe_float(candle.get("high"), 0)
        low = safe_float(candle.get("low"), 0)
        local = window[index - 2 : index + 3]
        if high >= max(safe_float(item.get("high"), 0) for item in local):
            pivot_highs.append(compact_level(candle, high))
        if low <= min(safe_float(item.get("low"), float("inf")) for item in local):
            pivot_lows.append(compact_level(candle, low))
    return {
        "supports": cluster_levels(pivot_lows),
        "resistances": cluster_levels(pivot_highs),
    }


def swing_points(candles: list[dict[str, Any]]) -> dict[str, Any]:
    window = candles[-min(len(candles), 120):]
    low = min(window, key=lambda item: safe_float(item.get("low"), float("inf")))
    high = max(window, key=lambda item: safe_float(item.get("high"), 0))
    return {
        "low": compact_candle(low),
        "high": compact_candle(high),
    }


def trend_candidates(candles: list[dict[str, Any]]) -> dict[str, Any]:
    swings = swing_points(candles)
    low = swings.get("low")
    high = swings.get("high")
    if not low or not high:
        return {}
    if str(low.get("timestamp")) <= str(high.get("timestamp")):
        return {"uptrend": {"anchors": [low, high], "reason": "recent low to recent high"}}
    return {"downtrend": {"anchors": [high, low], "reason": "recent high to recent low"}}


def volume_events(candles: list[dict[str, Any]]) -> dict[str, Any]:
    window = candles[-min(len(candles), 160):]
    average_volume = mean([safe_float(candle.get("volume"), 0) for candle in window]) if window else 0
    ranked = sorted(window, key=lambda candle: safe_float(candle.get("volume"), 0), reverse=True)[:5]
    return {
        "averageVolume": round(average_volume, 2),
        "events": [
            {
                **compact_candle(candle),
                "relativeVolume": round(safe_float(candle.get("volume"), 0) / average_volume, 2) if average_volume else 0,
            }
            for candle in ranked
        ],
    }


def ma_context(candles: list[dict[str, Any]]) -> dict[str, Any]:
    latest = candles[-1]
    close = safe_float(latest.get("close"), 0)
    moving_averages = {
        key: safe_float(latest.get(key), float("nan"))
        for key in ("ma5", "ma20", "ma60")
        if safe_float(latest.get(key), float("nan")) == safe_float(latest.get(key), float("nan"))
    }
    return {
        "latestClose": close,
        "movingAverages": moving_averages,
        "bias": {
            key: "above" if close >= value else "below"
            for key, value in moving_averages.items()
        },
    }


def cluster_levels(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not levels:
        return []
    ordered = sorted(levels, key=lambda item: safe_float(item.get("price"), 0))
    clusters: list[list[dict[str, Any]]] = []
    for level in ordered:
        price = safe_float(level.get("price"), 0)
        if not clusters:
            clusters.append([level])
            continue
        current_prices = [safe_float(item.get("price"), 0) for item in clusters[-1]]
        current_average = mean(current_prices)
        tolerance = max(0.05, current_average * 0.004)
        if abs(price - current_average) <= tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    ranked = sorted(clusters, key=lambda cluster: (len(cluster), latest_timestamp(cluster)), reverse=True)[:5]
    return [
        {
            "price": round(mean([safe_float(item.get("price"), 0) for item in cluster]), 4),
            "touches": len(cluster),
            "latestTimestamp": latest_timestamp(cluster),
        }
        for cluster in ranked
    ]


def latest_timestamp(levels: list[dict[str, Any]]) -> str:
    timestamps = [str(level.get("timestamp")) for level in levels if level.get("timestamp")]
    return max(timestamps) if timestamps else ""


def compact_level(candle: dict[str, Any], price: float) -> dict[str, Any]:
    return {
        "timestamp": candle.get("timestamp"),
        "price": round(price, 4),
        "volume": candle.get("volume"),
    }


def compact_candle(candle: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": candle.get("timestamp"),
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
        "volume": candle.get("volume"),
    }


def safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback
