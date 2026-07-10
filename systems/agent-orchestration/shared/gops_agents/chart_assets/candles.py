from __future__ import annotations

from typing import Any

from alfaka.analytics import LOOKBACK_BARS, normalize_candles
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider


class ChartAssetCandleLoader:
    def __init__(self, provider: Any | None = None):
        self.provider = provider or ClickHouseMarketDataProvider()

    def load(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        limit = LOOKBACK_BARS[interval]
        if interval == "1D":
            rows = self.provider.daily_candles(symbol, interval="1D", limit=limit)
        else:
            rows = self.provider.aggregated_daily_candles(symbol, interval, limit=limit)
        return normalize_candles(rows, interval)
