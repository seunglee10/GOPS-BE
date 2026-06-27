LOOKBACK_MINUTES_24H = 24 * 60
LOOKBACK_DAYS_1Y = 365
LOOKBACK_MINUTES_1Y = LOOKBACK_DAYS_1Y * LOOKBACK_MINUTES_24H
MAX_CHART_CANDLE_LIMIT = LOOKBACK_MINUTES_1Y

INTERVAL_MINUTES = {
    "1m": 1,
    "5m": 5,
    "10m": 10,
    "1d": LOOKBACK_MINUTES_24H,
}


def candle_count_for_24h(interval):
    minutes = INTERVAL_MINUTES.get(str(interval), 1)
    return max(1, LOOKBACK_MINUTES_24H // max(1, minutes))


def candle_count_for_1y(interval):
    minutes = INTERVAL_MINUTES.get(str(interval), 1)
    return max(1, LOOKBACK_MINUTES_1Y // max(1, minutes))


def resolve_candle_limit(interval, limit=None):
    if limit is None:
        return candle_count_for_24h(interval)
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        return candle_count_for_24h(interval)
    return max(1, min(resolved, MAX_CHART_CANDLE_LIMIT))
