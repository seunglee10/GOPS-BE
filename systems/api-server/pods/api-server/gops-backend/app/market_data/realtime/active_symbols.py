import logging
import os
import time

from alfaka.common.redis_keys import RedisKeyBuilder
from app.market_data.realtime.subscription_cohorts import (
    RealtimeSubscriptionCohortService,
)

logger = logging.getLogger(__name__)


class ActiveSymbolManager:
    def __init__(self, redis_client, ttl_seconds=None, refresh_seconds=5):
        self.redis = redis_client
        self.keys = RedisKeyBuilder()
        self.ttl_seconds = ttl_seconds or parse_int(os.getenv("ACTIVE_CHART_TTL_SECONDS"), 45)
        self.refresh_seconds = min(refresh_seconds, max(1, self.ttl_seconds // 3))
        self.error_log_interval_seconds = parse_int(os.getenv("ACTIVE_CHART_REFRESH_ERROR_LOG_INTERVAL_SECONDS"), 30)
        self._last_refresh = {}
        self._last_refresh_error_log = 0.0
        self.cohorts = RealtimeSubscriptionCohortService(redis_client, self.keys, auto_reconcile=False)

    def refresh(self, user_id, session_id, symbol):
        symbol = str(symbol).strip().upper()
        if not symbol:
            return False
        now = time.monotonic()
        refresh_key = f"{user_id}:{session_id}:{symbol}"
        if now - self._last_refresh.get(refresh_key, 0) < self.refresh_seconds:
            return False
        self._last_refresh[refresh_key] = now
        try:
            self.cohorts.refresh_active_chart(user_id, session_id, symbol, self.ttl_seconds)
        except Exception as exc:
            self._log_refresh_failure(symbol, exc)
            return False
        return True

    def close(self, user_id, session_id):
        try:
            self.cohorts.remove_active_chart(user_id, session_id)
        except Exception:
            return

    def _log_refresh_failure(self, symbol, exc):
        now = time.monotonic()
        if now - self._last_refresh_error_log < self.error_log_interval_seconds:
            return
        self._last_refresh_error_log = now
        logger.warning(
            "Realtime active chart refresh skipped: symbol=%s error=%s",
            symbol,
            exc,
        )


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
