import os


TRADING_MINUTES_PER_DAY = 390
TRADING_DAYS_PER_YEAR = 252
HISTORICAL_TARGET_YEARS = 6
INTRADAY_PRELOAD_TARGET_TRADING_DAYS = TRADING_DAYS_PER_YEAR * HISTORICAL_TARGET_YEARS
INTRADAY_PRELOAD_TARGET_DAYS = 365 * HISTORICAL_TARGET_YEARS
INTRADAY_PRELOAD_TARGET_BARS = TRADING_MINUTES_PER_DAY * INTRADAY_PRELOAD_TARGET_TRADING_DAYS
INTRADAY_PRELOAD_MIN_START_ENV = "BACKFILL_INITIAL_LOAD_1M_MIN_START"
DEFAULT_INTRADAY_PRELOAD_MIN_START = "2020-07-01T00:00:00Z"

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

BACKFILL_TARGET_BARS = {
    "1m": INTRADAY_PRELOAD_TARGET_BARS,
    "5m": (INTRADAY_PRELOAD_TARGET_BARS + 4) // 5,
    "10m": (INTRADAY_PRELOAD_TARGET_BARS + 9) // 10,
    "1D": TRADING_DAYS_PER_YEAR * HISTORICAL_TARGET_YEARS,
    "1W": 52 * HISTORICAL_TARGET_YEARS,
    "1M": 12 * HISTORICAL_TARGET_YEARS,
}

BACKFILL_TARGET_DAYS = {
    "1m": INTRADAY_PRELOAD_TARGET_DAYS,
    "5m": INTRADAY_PRELOAD_TARGET_DAYS,
    "10m": INTRADAY_PRELOAD_TARGET_DAYS,
    "1D": 365 * HISTORICAL_TARGET_YEARS,
    "1W": 365 * HISTORICAL_TARGET_YEARS,
    "1M": 365 * HISTORICAL_TARGET_YEARS,
}

REDIS_CLOSED_CANDLE_CAPS = {
    "1m": TRADING_MINUTES_PER_DAY * 2,
    "5m": (TRADING_MINUTES_PER_DAY * 2 + 4) // 5,
    "10m": (TRADING_MINUTES_PER_DAY * 2 + 9) // 10,
    "1D": TRADING_DAYS_PER_YEAR * HISTORICAL_TARGET_YEARS,
    "1W": 52 * HISTORICAL_TARGET_YEARS,
    "1M": 12 * HISTORICAL_TARGET_YEARS,
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
    interval: max(DEFAULT_VISIBLE_BARS[interval], BACKFILL_TARGET_BARS[interval])
    for interval in CHART_INTERVALS
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


def backfill_target_bars(interval):
    return BACKFILL_TARGET_BARS[normalize_chart_interval(interval)]


def backfill_target_days(interval):
    return BACKFILL_TARGET_DAYS[normalize_chart_interval(interval)]


def historical_target_bars(interval):
    return backfill_target_bars(interval)


def intraday_preload_min_start_iso():
    return (os.getenv(INTRADAY_PRELOAD_MIN_START_ENV) or DEFAULT_INTRADAY_PRELOAD_MIN_START).strip()


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
    return historical_target_bars(interval)


def resolve_candle_limit(interval, limit=None):
    interval = normalize_chart_interval(interval)
    if limit is None:
        return default_visible_bars(interval)
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        return default_visible_bars(interval)
    return max(1, min(resolved, max_request_bars(interval)))
