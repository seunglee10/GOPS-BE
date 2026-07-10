from __future__ import annotations

import inspect
import threading
import time
from collections import Counter
from typing import Any

from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from app.services.alfaka_market_data import normalize_market_symbol


class CanonicalCandleQuery:
    """Internal storage-first candle query boundary shared by every current caller."""

    def __init__(self, provider: Any, fill_service: Any):
        self.provider = provider
        self.fill_service = fill_service
        self.metrics = CanonicalQueryMetrics()

    def query(
        self,
        symbol: str,
        interval: str,
        limit: int | None,
        *,
        before: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        ma_windows: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        normalized_symbol = normalize_market_symbol(symbol)
        normalized_interval = normalize_chart_interval(interval)
        resolved_limit = resolve_candle_limit(normalized_interval, limit)
        started = time.monotonic()
        outcome = "error"
        try:
            payload = provider_candle_snapshot(
                self.provider,
                normalized_symbol,
                normalized_interval,
                resolved_limit,
                before=before,
                from_time=from_time,
                to_time=to_time,
                ma_windows=ma_windows,
            )
            payload = self.fill_service.fill_if_needed(
                symbol=normalized_symbol,
                interval=normalized_interval,
                limit=resolved_limit,
                before=before,
                from_time=from_time,
                to_time=to_time,
                payload=payload,
            )
            outcome = canonical_query_outcome(payload)
            return payload
        finally:
            self.metrics.record(outcome, time.monotonic() - started)


class CanonicalQueryMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._duration_ms = 0

    def record(self, outcome: str, duration_seconds: float) -> None:
        with self._lock:
            self._counts["requests"] += 1
            self._counts[outcome] += 1
            self._duration_ms += max(0, int(duration_seconds * 1000))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            requests = self._counts["requests"]
            return {
                "requests": requests,
                "outcomes": dict(self._counts),
                "durationMs": self._duration_ms,
                "averageDurationMs": self._duration_ms / requests if requests else 0,
            }


def canonical_query_outcome(payload: dict[str, Any]) -> str:
    fill = payload.get("fill") if isinstance(payload, dict) else None
    sources = fill.get("sources") if isinstance(fill, dict) else None
    if isinstance(sources, dict):
        for name in ("redis", "clickhouse", "s3", "alpaca"):
            source = sources.get(name)
            if isinstance(source, dict) and source.get("hit"):
                return name
    return "empty" if not payload.get("candles") else "provider"


def provider_candle_snapshot(
    provider: Any,
    symbol: str,
    interval: str,
    limit: int,
    *,
    before: str | None,
    from_time: str | None,
    to_time: str | None,
    ma_windows: tuple[int, ...],
) -> dict[str, Any]:
    kwargs = {"before": before, "from_time": from_time, "to_time": to_time}
    try:
        supports_ma = "ma_windows" in inspect.signature(provider.candle_snapshot).parameters
    except (TypeError, ValueError):
        supports_ma = False
    if supports_ma:
        kwargs["ma_windows"] = ma_windows
    return provider.candle_snapshot(symbol, interval, limit, **kwargs)
