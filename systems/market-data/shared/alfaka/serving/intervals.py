TRADING_MINUTES_PER_DAY = 390
TRADING_DAYS_PER_YEAR = 252

CHART_INTERVALS = ("1m", "5m", "10m", "1D", "1W", "1M")
LEGACY_INTERVALS = {"1d": "1D", "1w": "1W", "1mo": "1M", "1MO": "1M", "1month": "1M"}

DEFAULT_VISIBLE_BARS = {
    "1m": 390,
    "5m": 390,
    "10m": 390,
    "1D": 250,
    "1W": 260,
    "1M": 120,
}

REDIS_CLOSED_CANDLE_CAPS = {
    "1m": TRADING_MINUTES_PER_DAY * 2,
    "5m": (TRADING_MINUTES_PER_DAY * 2 + 4) // 5,
    "10m": (TRADING_MINUTES_PER_DAY * 2 + 9) // 10,
    "1D": TRADING_DAYS_PER_YEAR,
    "1W": 52,
    "1M": 24,
}

MIN_RENDERABLE_RETURNED_BARS = {
    "1m": 20,
    "5m": 10,
    "10m": 8,
    "1D": 30,
    "1W": 12,
    "1M": 6,
}

MIN_RENDERABLE_SOURCE_BARS = {
    "1m": 30,
    "1D": 60,
}

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "10m": 10 * 60,
    "1D": 24 * 60 * 60,
    "1W": 7 * 24 * 60 * 60,
    "1M": 31 * 24 * 60 * 60,
}

MAX_REQUEST_BARS = {
    "1m": 5000,
    "5m": 3000,
    "10m": 3000,
    "1D": 3000,
    "1W": 1000,
    "1M": 1000,
}

MAX_CHART_CANDLE_LIMIT = max(MAX_REQUEST_BARS.values())


def normalize_chart_interval(interval):
    value = str(interval or "1m").strip()
    value = LEGACY_INTERVALS.get(value, value)
    if value not in CHART_INTERVALS:
        raise ValueError(f"Unsupported chart interval: {interval}")
    return value


def default_visible_bars(interval):
    return DEFAULT_VISIBLE_BARS[normalize_chart_interval(interval)]


def redis_closed_candle_cap(interval):
    return REDIS_CLOSED_CANDLE_CAPS[normalize_chart_interval(interval)]


def max_request_bars(interval):
    return MAX_REQUEST_BARS[normalize_chart_interval(interval)]


def minimum_renderable_returned_bars(interval):
    return MIN_RENDERABLE_RETURNED_BARS[normalize_chart_interval(interval)]


def minimum_renderable_source_bars(interval):
    source_interval = source_interval_for(interval)
    return MIN_RENDERABLE_SOURCE_BARS.get(source_interval, minimum_renderable_returned_bars(source_interval))


def interval_seconds(interval):
    return INTERVAL_SECONDS[normalize_chart_interval(interval)]


def source_interval_for(interval):
    interval = normalize_chart_interval(interval)
    if interval in {"5m", "10m"}:
        return "1m"
    if interval in {"1W", "1M"}:
        return "1D"
    return interval


def is_derived_interval(interval):
    return normalize_chart_interval(interval) in {"5m", "10m", "1W", "1M"}


def candle_count_for_24h(interval):
    return default_visible_bars(interval)


def candle_count_for_1y(interval):
    interval = normalize_chart_interval(interval)
    if interval == "1m":
        return TRADING_MINUTES_PER_DAY * TRADING_DAYS_PER_YEAR
    if interval in {"5m", "10m"}:
        divisor = 5 if interval == "5m" else 10
        return (TRADING_MINUTES_PER_DAY * TRADING_DAYS_PER_YEAR + divisor - 1) // divisor
    if interval == "1D":
        return TRADING_DAYS_PER_YEAR
    if interval == "1W":
        return 52
    return 12


def resolve_candle_limit(interval, limit=None):
    interval = normalize_chart_interval(interval)
    if limit is None:
        return default_visible_bars(interval)
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        return default_visible_bars(interval)
    return max(1, min(resolved, max_request_bars(interval)))
