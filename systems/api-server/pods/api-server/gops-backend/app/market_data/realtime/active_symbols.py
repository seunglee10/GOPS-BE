import os
import time

from alfaka.common.redis_keys import RedisKeyBuilder


class ActiveSymbolManager:
    def __init__(self, redis_client, ttl_seconds=None, refresh_seconds=5):
        self.redis = redis_client
        self.keys = RedisKeyBuilder()
        self.ttl_seconds = ttl_seconds or parse_int(os.getenv("ACTIVE_CHART_TTL_SECONDS"), 45)
        self.refresh_seconds = min(refresh_seconds, max(1, self.ttl_seconds // 3))
        self._last_refresh = {}

    def refresh(self, symbol):
        now = time.monotonic()
        if now - self._last_refresh.get(symbol, 0) < self.refresh_seconds:
            return
        self.redis.sadd(self.keys.active_symbols(), symbol)
        self.redis.setex(self.keys.active_symbol(symbol), self.ttl_seconds, "1")
        self._last_refresh[symbol] = now
        self.cleanup()

    def cleanup(self):
        try:
            for symbol in list(self.redis.smembers(self.keys.active_symbols())):
                if not self.redis.exists(self.keys.active_symbol(symbol)):
                    self.redis.srem(self.keys.active_symbols(), symbol)
        except Exception:
            return


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
