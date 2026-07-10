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

    def __init__(
        self,
        redis_client,
        redis_keys,
        refresh_ms: int = 150,
        clock=None,
        redis_fallback: bool = True,
        pinned_symbols=None,
    ):
        self.redis = redis_client
        self.keys = redis_keys
        self.refresh_seconds = max(1, int(refresh_ms)) / 1000
        self.clock = clock or time.monotonic
        self.redis_fallback = redis_fallback
        self.pinned_symbols = frozenset(str(symbol).upper() for symbol in (pinned_symbols or []) if str(symbol).strip())
        self.memory_quotes: dict[str, dict[str, Any] | None] = {}
        self.redis_cache: dict[str, tuple[dict[str, Any] | None, float]] = {}

    def update(self, quote: dict[str, Any]) -> dict[str, Any] | None:
        normalized = str(quote.get("symbol") or "").upper()
        if not normalized:
            return None
        if self.pinned_symbols and normalized not in self.pinned_symbols:
            return None
        parsed = _normalize_quote(quote)
        self.memory_quotes[normalized] = parsed
        return parsed

    def quote_for(self, symbol: str) -> dict[str, Any] | None:
        normalized = str(symbol or "").upper()
        if normalized in self.memory_quotes:
            return self.memory_quotes[normalized]
        now = self.clock()
        cached = self.redis_cache.get(normalized)
        if cached is not None and now - cached[1] < self.refresh_seconds:
            return cached[0]
        if not self.redis_fallback:
            return None
        try:
            value = self.redis.get(self.keys.live_quote(normalized))
            quote = _parse_quote(value)
            self.redis_cache[normalized] = (quote, now)
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
    return _normalize_quote(raw)


def _normalize_quote(raw: dict[str, Any]) -> dict[str, Any]:
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
