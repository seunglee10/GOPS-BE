from __future__ import annotations

import json
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

try:
    import redis
except Exception:  # pragma: no cover - dependency guard for lean test envs
    redis = None  # type: ignore[assignment]


DEFAULT_ALERT_PREFIX = "alerts:v1"


class AlertProjectionError(RuntimeError):
    """Raised when the Redis alert projection cannot be read or written."""


class RedisAlertProjection:
    def __init__(self, redis_client: Any, prefix: str | None = None) -> None:
        self.redis = redis_client
        self.prefix = (prefix or os.getenv("ALERT_REDIS_KEY_PREFIX") or DEFAULT_ALERT_PREFIX).strip(":")
        self.config_channel = os.getenv("ALERT_CONFIG_CHANNEL", f"{self.prefix}:config.changed")

    @classmethod
    def from_env(cls) -> "RedisAlertProjection":
        if redis is None:
            raise AlertProjectionError("redis package is not installed")
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise AlertProjectionError("REDIS_URL is required for alert projection")
        return cls(redis.from_url(redis_url, decode_responses=True))

    def upsert_alert(self, alert: dict[str, Any]) -> None:
        alert_id = str(alert["id"])
        self.delete_alert(alert_id, symbol=alert.get("symbol"), publish=False)
        if alert.get("status") != "active":
            self._publish({"type": "delete", "alertId": alert_id, "symbol": alert.get("symbol")})
            return

        self.redis.hset(self._alerts_key(), alert_id, _dumps(alert))
        alert_type = alert.get("type")
        if alert_type == "price_cross":
            self.redis.zadd(self._price_key(str(alert["symbol"])), {alert_id: float(alert["target_price"])})
        elif alert_type == "spike":
            self.redis.zadd(self._spike_key(str(alert["symbol"])), {alert_id: float(alert["change_pct"])})
        self._publish({"type": "upsert", "alertId": alert_id, "symbol": alert.get("symbol"), "alertType": alert_type})

    def delete_alert(self, alert_id: int | str, *, symbol: str | None = None, publish: bool = True) -> None:
        alert_id_text = str(alert_id)
        existing = self.redis.hget(self._alerts_key(), alert_id_text)
        existing_alert = _loads(existing)
        resolved_symbol = symbol or (existing_alert.get("symbol") if existing_alert else None)
        if resolved_symbol:
            self.redis.zrem(self._price_key(str(resolved_symbol)), alert_id_text)
            self.redis.zrem(self._spike_key(str(resolved_symbol)), alert_id_text)
        self.redis.hdel(self._alerts_key(), alert_id_text)
        if publish:
            self._publish({"type": "delete", "alertId": alert_id_text, "symbol": resolved_symbol})

    def replace_all(self, active_alerts: list[dict[str, Any]]) -> None:
        for key in self.redis.scan_iter(f"{self.prefix}:price:*"):
            self.redis.delete(key)
        for key in self.redis.scan_iter(f"{self.prefix}:spike:*"):
            self.redis.delete(key)
        self.redis.delete(self._alerts_key())
        for alert in active_alerts:
            self.upsert_alert(alert)
        self._publish({"type": "reconcile", "count": len(active_alerts)})

    def price_cross_candidates(self, symbol: str, low: float, high: float) -> list[dict[str, Any]]:
        members = self.redis.zrangebyscore(self._price_key(symbol), low, high)
        return self._load_alerts(members)

    def spike_alerts(self, symbol: str) -> list[dict[str, Any]]:
        members = self.redis.zrange(self._spike_key(symbol), 0, -1)
        return self._load_alerts(members)

    def remember_price(self, symbol: str, price: float, timestamp_ms: int | None = None, *, retention_ms: int) -> None:
        timestamp_ms = timestamp_ms or int(time.time() * 1000)
        key = self._prices_key(symbol)
        self.redis.zadd(key, {_dumps({"timestampMs": timestamp_ms, "price": price}): timestamp_ms})
        self.redis.zremrangebyscore(key, 0, timestamp_ms - retention_ms)

    def baseline_price(self, symbol: str, since_ms: int) -> dict[str, Any] | None:
        rows = self.redis.zrangebyscore(self._prices_key(symbol), since_ms, "+inf", start=0, num=1)
        return _loads(rows[0]) if rows else None

    def last_price(self, symbol: str) -> float | None:
        value = self.redis.get(self._last_price_key(symbol))
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def set_last_price(self, symbol: str, price: float) -> None:
        self.redis.set(self._last_price_key(symbol), price)

    def mark_event_seen(self, event_id: str, ttl_seconds: int) -> bool:
        return bool(self.redis.set(self._dedupe_key(event_id), "1", ex=ttl_seconds, nx=True))

    def _load_alerts(self, alert_ids: list[str]) -> list[dict[str, Any]]:
        if not alert_ids:
            return []
        rows = self.redis.hmget(self._alerts_key(), alert_ids)
        return [alert for row in rows if (alert := _loads(row))]

    def _publish(self, payload: dict[str, Any]) -> None:
        self.redis.publish(self.config_channel, _dumps(payload))

    def _alerts_key(self) -> str:
        return f"{self.prefix}:alerts"

    def _price_key(self, symbol: str) -> str:
        return f"{self.prefix}:price:{symbol}"

    def _spike_key(self, symbol: str) -> str:
        return f"{self.prefix}:spike:{symbol}"

    def _prices_key(self, symbol: str) -> str:
        return f"{self.prefix}:prices:{symbol}"

    def _last_price_key(self, symbol: str) -> str:
        return f"{self.prefix}:last:{symbol}"

    def _dedupe_key(self, event_id: str) -> str:
        return f"{self.prefix}:dedupe:{event_id}"


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return value if isinstance(value, dict) else {}


def _dumps(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value
