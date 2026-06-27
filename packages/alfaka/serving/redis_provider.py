# 역할: GOPS API/WebSocket이 Redis에서 최신/최근 캔들을 읽는 adapter입니다.
# 사용: 과거 API는 최근 구간 보강에, WebSocket은 live candle push에 사용합니다.
# 계약: packages/alfaka/streaming/processor.py가 쓰는 Redis key를 그대로 읽습니다.
import json
import os

import redis

from alfaka.common.env import load_dotenv
from alfaka.serving.dto import snapshot, websocket_event


class RedisMarketDataProvider:
    def __init__(self, redis_url=None):
        load_dotenv()
        self.redis = redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

    def latest_price(self, symbol):
        return self.redis.hgetall(f"price:{symbol}:latest")

    def live_candle(self, symbol):
        value = self.redis.get(f"candle:{symbol}:1m:live")
        return json.loads(value) if value else None

    def recent_candles(self, symbol, interval, limit=160):
        rows = self.redis.zrevrange(f"candles:{symbol}:{interval}", 0, max(0, limit - 1))
        candles = [json.loads(row) for row in reversed(rows)]
        return candles

    def candle_snapshot(self, symbol, interval, limit=160):
        candles = self.recent_candles(symbol, interval, limit)
        return snapshot(symbol=symbol, interval=interval, candles=candles)

    def live_event(self, symbol):
        candle = self.live_candle(symbol)
        if not candle:
            return None
        return websocket_event("LIVE_CANDLE_UPDATE", symbol, "1m", candle)

    def closed_event(self, symbol, interval):
        value = self.redis.get(f"candle:{symbol}:{interval}:latest")
        if not value:
            return None
        candle = json.loads(value)
        event_type = "CANDLE_CORRECTED" if candle.get("correctionType") == "UPDATED" else "CANDLE_CLOSED"
        return websocket_event(event_type, symbol, interval, candle)
