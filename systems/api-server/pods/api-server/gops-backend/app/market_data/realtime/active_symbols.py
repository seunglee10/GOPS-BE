import os
import time

from alfaka.common.redis_keys import RedisKeyBuilder
from app.market_data.realtime.subscription_cohorts import (
    RealtimeSubscriptionCohortService,
)


class ActiveSymbolManager:
    def __init__(self, redis_client, ttl_seconds=None, refresh_seconds=5):
        self.redis = redis_client
        self.keys = RedisKeyBuilder()
        self.ttl_seconds = ttl_seconds or parse_int(os.getenv("ACTIVE_CHART_TTL_SECONDS"), 45)
        self.refresh_seconds = min(refresh_seconds, max(1, self.ttl_seconds // 3))
        self._last_refresh = {}
        self.cohorts = RealtimeSubscriptionCohortService(redis_client, self.keys, auto_reconcile=False)

    def refresh(self, user_id, session_id, symbol):
        symbol = str(symbol).strip().upper()
        if not symbol:
            return
        now = time.monotonic()
        refresh_key = f"{user_id}:{session_id}:{symbol}"
        if now - self._last_refresh.get(refresh_key, 0) < self.refresh_seconds:
            return
        self.cohorts.refresh_active_chart(user_id, session_id, symbol, self.ttl_seconds)
        self._last_refresh[refresh_key] = now

    def close(self, user_id, session_id):
        try:
            self.cohorts.remove_active_chart(user_id, session_id)
        except Exception:
            return


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
