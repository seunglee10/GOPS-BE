from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from alfaka.analytics import LOOKBACK_BARS, normalize_candles
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.time_utils import parse_utc_time


class ChartAssetCandleLoader:
    def __init__(
        self,
        provider: Any | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.provider = provider or ClickHouseMarketDataProvider()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def load(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        limit = LOOKBACK_BARS[interval]
        if interval == "1D":
            rows = self.provider.daily_candles(symbol, interval="1D", limit=limit)
        else:
            rows = self.provider.aggregated_daily_candles(symbol, interval, limit=limit, limit_buffer=1)
            rows = _completed_aggregate_rows(rows, interval, self.now_provider())
        return normalize_candles(rows, interval)


def _completed_aggregate_rows(
    rows: list[dict[str, Any]],
    interval: str,
    now: datetime,
) -> list[dict[str, Any]]:
    reference = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    completed: list[dict[str, Any]] = []
    for row in rows:
        bucket = parse_utc_time(row.get("timestamp"))
        if bucket is None:
            continue
        if interval == "1W":
            period_end = bucket + timedelta(days=7)
        elif interval == "1M":
            month = bucket.month + 1
            period_end = bucket.replace(
                year=bucket.year + (1 if month == 13 else 0),
                month=1 if month == 13 else month,
            )
        else:
            raise ValueError(f"Unsupported aggregated chart asset interval: {interval}")
        if period_end <= reference:
            completed.append(row)
    return completed
