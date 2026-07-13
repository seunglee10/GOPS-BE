from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from alfaka.analytics.analysis_candles import AnalysisCandleSource
from alfaka.analytics.geometry import SUPPORTED_INTERVALS
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider


class ChartAssetCandleLoader:
    def __init__(
        self,
        provider: Any | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.provider = provider or ClickHouseMarketDataProvider()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.analysis_source = AnalysisCandleSource(self.provider, now_provider=self.now_provider)

    def load_symbol(self, symbol: str, intervals: tuple[str, ...] | list[str]):
        return self.analysis_source.load_symbol(symbol, intervals)

    def load(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported geometry interval: {interval}")
        return list(self.load_symbol(symbol, [interval]).rows[interval])
