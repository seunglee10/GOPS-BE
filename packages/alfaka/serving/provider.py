# 역할: GOPS Chart API가 Redis와 ClickHouse를 함께 읽을 수 있게 묶는 provider입니다.
# 사용: 먼저 Redis 최근 캔들을 보고, 부족하면 ClickHouse 과거 캔들을 조회합니다.
# 결과: GOPS CandleSnapshot 형식으로 반환합니다.
import logging

from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.cursors import timestamp_from_cursor
from alfaka.serving.dto import cursor_for, snapshot
from alfaka.serving.redis_provider import RedisMarketDataProvider
from alfaka.serving.symbol_registry import SymbolRegistry


logger = logging.getLogger(__name__)


class MarketDataProvider:
    def __init__(self, redis_provider=None, clickhouse_provider=None):
        self.redis_provider = redis_provider or RedisMarketDataProvider()
        self.clickhouse_provider = clickhouse_provider or ClickHouseMarketDataProvider()
        self.symbol_registry = SymbolRegistry(self.clickhouse_provider, self.redis_provider)

    def candle_snapshot(self, symbol, interval, limit=160):
        redis_candles = self.redis_provider.recent_candles(symbol, interval, limit)
        if len(redis_candles) >= limit:
            return snapshot(symbol=symbol, interval=interval, candles=redis_candles[-limit:])

        clickhouse_candles = self.clickhouse_provider.candles(symbol, interval, limit)
        merged = merge_candles(clickhouse_candles, redis_candles)
        return snapshot(symbol=symbol, interval=interval, candles=merged[-limit:])

    def candles_since_cursor(self, symbol, interval, cursor, limit=500):
        timestamp = timestamp_from_cursor(cursor)
        redis_candles = [
            candle for candle in self.redis_provider.recent_candles(symbol, interval, limit)
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ]
        try:
            clickhouse_candles = self._clickhouse_candles_since_cursor(symbol, interval, timestamp, limit)
        except Exception:
            logger.warning("ClickHouse candles_since failed; falling back to Redis recent candles.", exc_info=True)
            clickhouse_candles = []
        filtered_clickhouse = [
            candle for candle in clickhouse_candles
            if candle_after_cursor(symbol, interval, candle, cursor, timestamp)
        ]
        return merge_candles(filtered_clickhouse, redis_candles)[-limit:]

    def _clickhouse_candles_since_cursor(self, symbol, interval, timestamp, limit):
        if not timestamp:
            return []
        try:
            return self.clickhouse_provider.candles_since(symbol, interval, timestamp, limit, include_from=True)
        except TypeError:
            return self.clickhouse_provider.candles_since(symbol, interval, timestamp, limit)

    def search_symbols(self, query, limit=20):
        return self.symbol_registry.search(query, limit)

    def symbol_detail(self, symbol):
        return self.symbol_registry.detail(symbol)

    def latest_status(self, symbol=None):
        return self.redis_provider.latest_status(symbol) or self.clickhouse_provider.latest_status(symbol)

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size=None):
        try:
            bins = self.clickhouse_provider.volume_profile_bins(symbol, from_time, to_time, None if price_bin_size == "auto" else price_bin_size)
        except Exception:
            logger.warning("ClickHouse volume_profile_bins failed; falling back to Redis live bins.", exc_info=True)
            bins = []
        if not bins:
            bins = self.redis_provider.volume_profile_bins(symbol)
        resolved_size = first_value(bins, "priceBinSize", 0.05)
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "timeBucket": "1m",
            "priceBinSize": resolved_size,
            "source": first_value(bins, "source", "clickhouse"),
            "feed": first_value(bins, "feed", "unknown"),
            "bins": bins,
        }

    def agent_chart_context(self, symbol, interval, from_time, to_time, include):
        candles = self.clickhouse_provider.candles_since(symbol, interval, from_time, 500)
        daily = self.clickhouse_provider.candles(symbol, "1d", 2)
        return {
            "symbol": symbol,
            "interval": interval,
            "visibleRange": {"from": from_time, "to": to_time},
            "candles": candles,
            "latestDailyCandle": daily[-1] if daily else None,
            "previousDailyCandle": daily[-2] if len(daily) > 1 else None,
            "marketStatus": self.latest_status(symbol) if "status" in include else None,
            "volumeProfile": self.volume_profile_bins(symbol, from_time, to_time, "auto") if "volumeProfile" in include else None,
            "comparisonCandidates": self.search_symbols(symbol[:2], 5),
        }


def merge_candles(*groups):
    by_timestamp = {}
    for group in groups:
        for candle in group:
            timestamp = candle.get("timestamp")
            if timestamp:
                by_timestamp[timestamp] = candle
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def candle_after_cursor(symbol, interval, candle, cursor, cursor_timestamp=None):
    if not cursor:
        return True
    cursor_timestamp = cursor_timestamp if cursor_timestamp is not None else timestamp_from_cursor(cursor)
    candle_timestamp = candle.get("timestamp") or candle.get("eventTime")
    if not candle_timestamp or not cursor_timestamp:
        return True
    if candle_timestamp > cursor_timestamp:
        return True
    if candle_timestamp < cursor_timestamp:
        return False
    return cursor_for(symbol, interval, candle) != cursor


def first_value(rows, key, fallback):
    for row in rows:
        value = row.get(key)
        if value:
            return value
    return fallback
