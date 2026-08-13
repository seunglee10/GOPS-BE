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
        self.delete_alert(alert_id, symbol=alert.get("symbol"), publish=False, preserve_state=True)
        if alert.get("status") != "active":
            self._publish({"type": "delete", "alertId": alert_id, "symbol": alert.get("symbol")})
            return

        self.redis.hset(self._alerts_key(), alert_id, _dumps(alert))
        alert_type = alert.get("type")
        if alert_type == "price_cross":
            self.redis.zadd(self._price_key(str(alert["symbol"])), {alert_id: float(alert["target_price"])})
        elif alert_type == "spike":
            self.redis.zadd(self._spike_key(str(alert["symbol"])), {alert_id: float(alert["change_pct"])})
        elif alert_type in {"volume_absolute", "volume_relative", "rsi_threshold"}:
            condition = alert.get("condition") if isinstance(alert.get("condition"), dict) else {}
            interval = str(condition.get("interval") or "1D")
            self.redis.sadd(self._metric_key(str(alert["symbol"]), interval), alert_id)
        self._publish({"type": "upsert", "alertId": alert_id, "symbol": alert.get("symbol"), "alertType": alert_type})

    def delete_alert(
        self,
        alert_id: int | str,
        *,
        symbol: str | None = None,
        publish: bool = True,
        preserve_state: bool = False,
    ) -> None:
        alert_id_text = str(alert_id)
        existing = self.redis.hget(self._alerts_key(), alert_id_text)
        existing_alert = _loads(existing)
        resolved_symbol = symbol or (existing_alert.get("symbol") if existing_alert else None)
        if resolved_symbol:
            self.redis.zrem(self._price_key(str(resolved_symbol)), alert_id_text)
            self.redis.zrem(self._spike_key(str(resolved_symbol)), alert_id_text)
            condition = existing_alert.get("condition") if isinstance(existing_alert.get("condition"), dict) else {}
            interval = str(condition.get("interval") or "1D")
            self.redis.srem(self._metric_key(str(resolved_symbol), interval), alert_id_text)
        self.redis.hdel(self._alerts_key(), alert_id_text)
        if not preserve_state:
            self.redis.delete(self._condition_state_key(alert_id_text))
        if publish:
            self._publish({"type": "delete", "alertId": alert_id_text, "symbol": resolved_symbol})

    def replace_all(self, active_alerts: list[dict[str, Any]]) -> None:
        for key in self.redis.scan_iter(f"{self.prefix}:price:*"):
            self.redis.delete(key)
        for key in self.redis.scan_iter(f"{self.prefix}:spike:*"):
            self.redis.delete(key)
        for key in self.redis.scan_iter(f"{self.prefix}:metric:*"):
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

    def metric_alerts(self, symbol: str, interval: str) -> list[dict[str, Any]]:
        members = sorted(self.redis.smembers(self._metric_key(symbol, interval)))
        return self._load_alerts(members)

    def remember_candle(self, candle: dict[str, Any], *, retention_bars: int = 240) -> list[dict[str, Any]]:
        symbol = str(candle["symbol"])
        interval = str(candle["interval"])
        timestamp_ms = int(candle["timestampMs"])
        key = self._candles_key(symbol, interval)
        self.redis.zremrangebyscore(key, timestamp_ms, timestamp_ms)
        self.redis.zadd(key, {_dumps(candle): timestamp_ms})
        count = int(self.redis.zcard(key))
        if count > retention_bars:
            self.redis.zremrangebyrank(key, 0, count - retention_bars - 1)
        rows = self.redis.zrange(key, 0, -1)
        return [item for row in rows if (item := _loads(row))]

    def condition_state(self, alert_id: int | str) -> bool | None:
        value = self.redis.get(self._condition_state_key(alert_id))
        if value is None:
            return None
        return str(value) == "1"

    def set_condition_state(self, alert_id: int | str, satisfied: bool) -> None:
        self.redis.set(self._condition_state_key(alert_id), "1" if satisfied else "0")

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

    def _metric_key(self, symbol: str, interval: str) -> str:
        return f"{self.prefix}:metric:{symbol}:{interval}"

    def _candles_key(self, symbol: str, interval: str) -> str:
        return f"{self.prefix}:candles:{symbol}:{interval}"

    def _condition_state_key(self, alert_id: int | str) -> str:
        return f"{self.prefix}:state:{alert_id}"

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
