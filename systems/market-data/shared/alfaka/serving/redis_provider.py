# 역할: GOPS API/WebSocket이 Redis에서 최신/최근 캔들을 읽는 adapter입니다.
# 사용: 과거 API는 최근 구간 보강에, WebSocket은 live candle push에 사용합니다.
# 계약: systems/market-data/shared/alfaka/streaming/processor.py가 쓰는 Redis key를 그대로 읽습니다.
import json
import os

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.dto import snapshot, websocket_event
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from alfaka.serving.moving_average import attach_moving_averages


class RedisMarketDataProvider:
    def __init__(self, redis_url=None):
        load_dotenv()
        self.redis = redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        self.keys = RedisKeyBuilder()

    def latest_price(self, symbol):
        return self.redis.hgetall(self.keys.price_latest(symbol))

    def live_candle(self, symbol, interval="1m"):
        interval = normalize_chart_interval(interval)
        value = self.redis.get(self.keys.live_candle(symbol, interval))
        return json.loads(value) if value else None

    def recent_candles(self, symbol, interval, limit=None):
        interval = normalize_chart_interval(interval)
        limit = resolve_candle_limit(interval, limit)
        rows = self.redis.zrevrange(self.keys.recent_candles(symbol, interval), 0, max(0, limit - 1))
        candles = [json.loads(row) for row in reversed(rows)]
        return candles

    def candle_snapshot(self, symbol, interval, limit=None):
        interval = normalize_chart_interval(interval)
        candles = attach_moving_averages(self.recent_candles(symbol, interval, limit))
        return snapshot(symbol=symbol, interval=interval, candles=candles)

    def live_event(self, symbol, interval="1m"):
        interval = normalize_chart_interval(interval)
        candle = self.live_candle(symbol, interval)
        if not candle:
            return None
        return websocket_event("LIVE_CANDLE_UPDATE", symbol, interval, candle)

    def closed_event(self, symbol, interval):
        interval = normalize_chart_interval(interval)
        value = self.redis.get(self.keys.latest_candle(symbol, interval))
        if not value:
            return None
        candle = json.loads(value)
        event_type = "CANDLE_CORRECTED" if candle.get("correctionType") == "UPDATED" else "CANDLE_CLOSED"
        return websocket_event(event_type, symbol, interval, candle)

    def latest_status(self, symbol=None):
        key = self.keys.market_status_symbol_latest(symbol) if symbol else self.keys.market_status_latest()
        value = self.redis.get(key)
        return json.loads(value) if value else None

    def volume_profile_bins(self, symbol, from_score="-inf", to_score="+inf", limit=5000):
        rows = self.redis.zrangebyscore(self.keys.volume_profile_live(symbol), from_score, to_score, start=0, num=limit)
        return [json.loads(row) for row in rows]

    def symbol_metadata(self, symbol):
        value = self.redis.get(self.keys.symbol_metadata(symbol))
        return json.loads(value) if value else None

    def hot_symbols_snapshot(self):
        value = self.redis.get(self.keys.hot_symbols_snapshot())
        return json.loads(value) if value else None

    def backfill_no_data_before(self, symbol, interval):
        interval = normalize_chart_interval(interval)
        return self.redis.get(self.keys.backfill_no_data_before(symbol, interval))
