from __future__ import annotations

import os


DEFAULT_ORDER_FLOW_PINNED_SYMBOLS = "NVDA,AMZN,MU,AAPL,GOOGL"
DEFAULT_ORDER_FLOW_PRICE_BIN_SIZE = 0.01
DEFAULT_ORDER_FLOW_QUOTE_REFRESH_MS = 150
DEFAULT_ORDER_FLOW_PUBLISH_THROTTLE_MS = 250
DEFAULT_ORDER_FLOW_LIVE_TTL_SECONDS = 86400


def pinned_symbols_from_env() -> frozenset[str]:
    raw = os.getenv("ORDER_FLOW_PINNED_SYMBOLS", DEFAULT_ORDER_FLOW_PINNED_SYMBOLS)
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return frozenset(symbols)


def price_bin_size_from_env() -> float:
    return _float_env("ORDER_FLOW_PRICE_BIN_SIZE", DEFAULT_ORDER_FLOW_PRICE_BIN_SIZE)


def quote_refresh_ms_from_env() -> int:
    return _int_env("ORDER_FLOW_QUOTE_REFRESH_MS", DEFAULT_ORDER_FLOW_QUOTE_REFRESH_MS)


def publish_throttle_ms_from_env() -> int:
    return _int_env("ORDER_FLOW_PUBLISH_THROTTLE_MS", DEFAULT_ORDER_FLOW_PUBLISH_THROTTLE_MS)


def live_ttl_seconds_from_env() -> int:
    return _int_env("ORDER_FLOW_LIVE_TTL_SECONDS", DEFAULT_ORDER_FLOW_LIVE_TTL_SECONDS)


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
