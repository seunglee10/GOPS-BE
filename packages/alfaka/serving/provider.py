# 역할: GOPS Chart API가 Redis와 ClickHouse를 함께 읽을 수 있게 묶는 provider입니다.
# 사용: 먼저 Redis 최근 캔들을 보고, 부족하면 ClickHouse 과거 캔들을 조회합니다.
# 결과: GOPS CandleSnapshot 형식으로 반환합니다.
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.dto import snapshot
from alfaka.serving.redis_provider import RedisMarketDataProvider


class MarketDataProvider:
    def __init__(self, redis_provider=None, clickhouse_provider=None):
        self.redis_provider = redis_provider or RedisMarketDataProvider()
        self.clickhouse_provider = clickhouse_provider or ClickHouseMarketDataProvider()

    def candle_snapshot(self, symbol, interval, limit=160):
        redis_candles = self.redis_provider.recent_candles(symbol, interval, limit)
        if len(redis_candles) >= limit:
            return snapshot(symbol=symbol, interval=interval, candles=redis_candles[-limit:])

        clickhouse_candles = self.clickhouse_provider.candles(symbol, interval, limit)
        merged = merge_candles(clickhouse_candles, redis_candles)
        return snapshot(symbol=symbol, interval=interval, candles=merged[-limit:])


def merge_candles(*groups):
    by_timestamp = {}
    for group in groups:
        for candle in group:
            timestamp = candle.get("timestamp")
            if timestamp:
                by_timestamp[timestamp] = candle
    return [by_timestamp[key] for key in sorted(by_timestamp)]
