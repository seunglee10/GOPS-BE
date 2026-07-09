from __future__ import annotations

import json
import time
from typing import Any


class PinnedQuoteCache:
    """Short-lived Redis L1 quote cache used for live estimated side classification.

    Live classification can use a quote up to roughly refresh_ms plus Redis write latency stale.
    The EOD rollup recomputes daily profiles with an exact as-of join, so persisted daily data does
    not depend on this cache.
    """

    def __init__(self, redis_client, redis_keys, refresh_ms: int = 150, clock=None):
        self.redis = redis_client
        self.keys = redis_keys
        self.refresh_seconds = max(1, int(refresh_ms)) / 1000
        self.clock = clock or time.monotonic
        self.cache: dict[str, tuple[dict[str, Any] | None, float]] = {}

    def quote_for(self, symbol: str) -> dict[str, Any] | None:
        normalized = str(symbol or "").upper()
        now = self.clock()
        cached = self.cache.get(normalized)
        if cached is not None and now - cached[1] < self.refresh_seconds:
            return cached[0]
        try:
            value = self.redis.get(self.keys.live_quote(normalized))
            quote = _parse_quote(value)
            self.cache[normalized] = (quote, now)
            return quote
        except Exception:
            if cached is not None and now - cached[1] < 5:
                return cached[0]
            return None


def _parse_quote(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    raw = json.loads(value) if isinstance(value, str) else value
    if not isinstance(raw, dict):
        return None
    return {
        **raw,
        "bidPrice": _number_or_none(raw.get("bidPrice", raw.get("bid_price"))),
        "askPrice": _number_or_none(raw.get("askPrice", raw.get("ask_price"))),
        "bidSize": _number_or_none(raw.get("bidSize", raw.get("bid_size"))),
        "askSize": _number_or_none(raw.get("askSize", raw.get("ask_size"))),
    }


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
