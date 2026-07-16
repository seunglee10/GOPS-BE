from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from alfaka.analytics.analysis_candles import AnalysisCandleSource
from alfaka.analytics.geometry import SUPPORTED_INTERVALS
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.provider import MarketDataProvider


class _CanonicalSnapshotAdapter:
    """Expose the chart Redis+ClickHouse snapshot through AnalysisCandleSource."""

    def __init__(self, provider: Any):
        self.provider = provider

    def stored_interval_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        return self._candles(
            symbol, interval, limit=limit, before=before, from_time=from_time, to_time=to_time,
        )

    def daily_candles(self, symbol, interval="1D", limit=None, before=None, from_time=None, to_time=None):
        return self._candles(
            symbol, interval, limit=limit, before=before, from_time=from_time, to_time=to_time,
        )

    def _candles(self, symbol, interval, *, limit=None, before=None, from_time=None, to_time=None):
        payload = self.provider.candle_snapshot(
            symbol,
            interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
            ma_windows=(),
        )
        rows = payload.get("candles") if isinstance(payload, dict) else None
        return [dict(row) for row in rows] if isinstance(rows, list) else []


class ChartAssetCandleLoader:
    def __init__(
        self,
        provider: Any | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.provider = provider or MarketDataProvider()
        self.repair_provider = getattr(self.provider, "clickhouse_provider", self.provider)
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        analysis_provider = (
            _CanonicalSnapshotAdapter(self.provider)
            if callable(getattr(self.provider, "candle_snapshot", None))
            else self.provider
        )
        self.analysis_source = AnalysisCandleSource(
            analysis_provider,
            now_provider=self.now_provider,
            view="chart_completed" if analysis_provider is not self.provider else "analysis_closed",
        )

    def load_symbol(self, symbol: str, intervals: tuple[str, ...] | list[str]):
        return self.analysis_source.load_symbol(symbol, intervals)

    def load(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported geometry interval: {interval}")
        return list(self.load_symbol(symbol, [interval]).rows[interval])
