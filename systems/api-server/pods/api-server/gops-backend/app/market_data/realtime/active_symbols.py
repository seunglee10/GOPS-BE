import os
import time

from alfaka.common.redis_keys import RedisKeyBuilder


DEFAULT_REALTIME_LAYERS = ("trades", "quotes")
ACTIVE_CHART_SUBSCRIPTION_REASON = "active-chart-session"


class ActiveSymbolManager:
    def __init__(self, redis_client, ttl_seconds=None, refresh_seconds=5):
        self.redis = redis_client
        self.keys = RedisKeyBuilder()
        self.ttl_seconds = ttl_seconds or parse_int(os.getenv("ACTIVE_CHART_TTL_SECONDS"), 45)
        self.refresh_seconds = min(refresh_seconds, max(1, self.ttl_seconds // 3))
        self._last_refresh = {}

    def refresh(self, symbol):
        symbol = str(symbol).strip().upper()
        if not symbol:
            return
        now = time.monotonic()
        if now - self._last_refresh.get(symbol, 0) < self.refresh_seconds:
            return
        self.redis.sadd(self.keys.active_symbols(), symbol)
        self.redis.setex(self.keys.active_symbol(symbol), self.ttl_seconds, "1")
        self.refresh_realtime_subscription(symbol)
        self._last_refresh[symbol] = now
        self.cleanup()

    def refresh_realtime_subscription(self, symbol):
        key = self.keys.subscription_symbol(symbol)
        existing = self._hgetall(key)
        existing_layers = self._layers(existing.get("layers"))
        layers = sorted(existing_layers.union(DEFAULT_REALTIME_LAYERS))
        record = {
            "symbol": symbol,
            "enabled": "true",
            "layers": ",".join(layers),
            "reason": existing.get("reason") or ACTIVE_CHART_SUBSCRIPTION_REASON,
            "source": existing.get("source") or "chart-websocket",
            "ttlSeconds": str(self.ttl_seconds),
            "updatedAt": str(int(time.time())),
        }
        self.redis.sadd(self.keys.subscription_symbols(), symbol)
        self.redis.hset(key, mapping=record)
        self.redis.expire(key, self.ttl_seconds)

    def cleanup(self):
        try:
            for symbol in list(self.redis.smembers(self.keys.active_symbols())):
                if not self.redis.exists(self.keys.active_symbol(symbol)):
                    self.redis.srem(self.keys.active_symbols(), symbol)
                    self._cleanup_active_chart_subscription(symbol)
        except Exception:
            return

    def _cleanup_active_chart_subscription(self, symbol):
        key = self.keys.subscription_symbol(symbol)
        record = self._hgetall(key)
        if record and record.get("reason") != ACTIVE_CHART_SUBSCRIPTION_REASON:
            return
        try:
            self.redis.srem(self.keys.subscription_symbols(), symbol)
            self.redis.delete(key)
        except Exception:
            return

    def _hgetall(self, key):
        try:
            raw = self.redis.hgetall(key) or {}
        except Exception:
            return {}
        return {self._decode(k): self._decode(v) for k, v in raw.items()}

    @staticmethod
    def _decode(value):
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    @staticmethod
    def _layers(raw_layers):
        return {item.strip() for item in str(raw_layers or "").split(",") if item.strip()}


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
