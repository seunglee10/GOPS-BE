from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from market_data.analytics.analysis_candles import (
    AnalysisCandleBundle,
    AnalysisCandleSource,
    analysis_input_digest,
    compute_analysis_coverage,
)
from market_data.analytics.geometry import SUPPORTED_INTERVALS, TARGET_BARS
from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider
from market_data.serving.provider import MarketDataProvider


class _CanonicalSnapshotAdapter:
    """Expose the chart Redis+ClickHouse snapshot through AnalysisCandleSource."""

    def __init__(self, provider: Any, *, cutoff: datetime | None = None):
        self.provider = provider
        self.cutoff = cutoff

    def stored_interval_candles(self, symbol, interval, limit=None, before=None, from_time=None, to_time=None):
        return self._candles(
            symbol, interval, limit=limit, before=before, from_time=from_time, to_time=to_time,
        )

    def daily_candles(self, symbol, interval="1D", limit=None, before=None, from_time=None, to_time=None):
        return self._candles(
            symbol, interval, limit=limit, before=before, from_time=from_time, to_time=to_time,
        )

    def _candles(self, symbol, interval, *, limit=None, before=None, from_time=None, to_time=None):
        cutoff = self.cutoff.isoformat() if self.cutoff is not None else None
        payload = self.provider.candle_snapshot(
            symbol,
            interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=cutoff or to_time,
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

    def load_symbol_at(
        self,
        symbol: str,
        intervals: tuple[str, ...] | list[str],
        cutoff: datetime,
    ):
        normalized_cutoff = cutoff.astimezone(timezone.utc)
        analysis_provider = (
            _CanonicalSnapshotAdapter(self.provider, cutoff=normalized_cutoff)
            if callable(getattr(self.provider, "candle_snapshot", None))
            else self.provider
        )
        source = AnalysisCandleSource(
            analysis_provider,
            now_provider=lambda: normalized_cutoff,
            view="chart_completed" if analysis_provider is not self.provider else "analysis_closed",
        )
        bundle = source.load_symbol(symbol, intervals)
        # Providers without a bounded snapshot API are still prevented from
        # leaking rows beyond the frozen simulation cutoff.
        rows = {
            interval: [
                row for row in bundle.rows[interval]
                if _timestamp(row.get("timestamp")) <= normalized_cutoff
            ]
            for interval in intervals
        }
        return AnalysisCandleBundle(
            rows=rows,
            coverage={
                interval: compute_analysis_coverage(
                    rows[interval], interval, display_bars=TARGET_BARS[interval], now=normalized_cutoff,
                )
                for interval in intervals
            },
            digests={
                interval: analysis_input_digest(symbol, interval, rows[interval])
                for interval in intervals
            },
        )

    def load(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported geometry interval: {interval}")
        return list(self.load_symbol(symbol, [interval]).rows[interval])


def _timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
