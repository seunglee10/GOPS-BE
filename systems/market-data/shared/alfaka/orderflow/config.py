from __future__ import annotations

import os


DEFAULT_ORDER_FLOW_PINNED_SYMBOLS = "NVDA,AMZN,MU,AAPL,GOOGL"
DEFAULT_ORDER_FLOW_PRICE_BIN_SIZE = 0.01
DEFAULT_ORDER_FLOW_QUOTE_REFRESH_MS = 150
DEFAULT_ORDER_FLOW_QUOTE_MAX_AGE_MS = 2000
DEFAULT_ORDER_FLOW_QUOTE_FUTURE_TOLERANCE_MS = 250
DEFAULT_ORDER_FLOW_PUBLISH_THROTTLE_MS = 250
DEFAULT_ORDER_FLOW_REDIS_FLUSH_MS = 250
DEFAULT_ORDER_FLOW_LIVE_TTL_SECONDS = 86400
DEFAULT_ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS = 300
DEFAULT_QUOTE_REDIS_WRITE_MIN_INTERVAL_MS = 100
DEFAULT_QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS = 250
DEFAULT_TRADE_REDIS_WRITE_MIN_INTERVAL_MS = 250
DEFAULT_HEALTH_WRITE_MIN_INTERVAL_MS = 1000


def pinned_symbols_from_env() -> frozenset[str]:
    raw = os.getenv("ORDER_FLOW_PINNED_SYMBOLS", DEFAULT_ORDER_FLOW_PINNED_SYMBOLS)
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return frozenset(symbols)


def price_bin_size_from_env() -> float:
    return _float_env("ORDER_FLOW_PRICE_BIN_SIZE", DEFAULT_ORDER_FLOW_PRICE_BIN_SIZE)


def quote_refresh_ms_from_env() -> int:
    return _int_env("ORDER_FLOW_QUOTE_REFRESH_MS", DEFAULT_ORDER_FLOW_QUOTE_REFRESH_MS)


def quote_max_age_ms_from_env() -> int:
    return _int_env("ORDER_FLOW_QUOTE_MAX_AGE_MS", DEFAULT_ORDER_FLOW_QUOTE_MAX_AGE_MS)


def quote_future_tolerance_ms_from_env() -> int:
    return _non_negative_int_env("ORDER_FLOW_QUOTE_FUTURE_TOLERANCE_MS", DEFAULT_ORDER_FLOW_QUOTE_FUTURE_TOLERANCE_MS)


def publish_throttle_ms_from_env() -> int:
    return _int_env("ORDER_FLOW_PUBLISH_THROTTLE_MS", DEFAULT_ORDER_FLOW_PUBLISH_THROTTLE_MS)


def redis_flush_ms_from_env() -> int:
    return _int_env("ORDER_FLOW_REDIS_FLUSH_MS", DEFAULT_ORDER_FLOW_REDIS_FLUSH_MS)


def live_ttl_seconds_from_env() -> int:
    return _int_env("ORDER_FLOW_LIVE_TTL_SECONDS", DEFAULT_ORDER_FLOW_LIVE_TTL_SECONDS)


def live_minute_ttl_seconds_from_env() -> int:
    return _int_env("ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS", DEFAULT_ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS)


def quote_redis_write_min_interval_ms_from_env() -> int:
    return _non_negative_int_env("QUOTE_REDIS_WRITE_MIN_INTERVAL_MS", DEFAULT_QUOTE_REDIS_WRITE_MIN_INTERVAL_MS)


def quote_event_publish_min_interval_ms_from_env() -> int:
    return _non_negative_int_env("QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS", DEFAULT_QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS)


def trade_redis_write_min_interval_ms_from_env() -> int:
    return _non_negative_int_env("TRADE_REDIS_WRITE_MIN_INTERVAL_MS", DEFAULT_TRADE_REDIS_WRITE_MIN_INTERVAL_MS)


def health_write_min_interval_ms_from_env() -> int:
    return _non_negative_int_env("HEALTH_WRITE_MIN_INTERVAL_MS", DEFAULT_HEALTH_WRITE_MIN_INTERVAL_MS)


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _non_negative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default
