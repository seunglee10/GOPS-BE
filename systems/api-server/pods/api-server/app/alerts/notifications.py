from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from typing import Any, AsyncIterator

try:
    import redis
except Exception:  # pragma: no cover - dependency guard for lean test envs
    redis = None  # type: ignore[assignment]


class NotificationBrokerError(RuntimeError):
    """Raised when the notification broker cannot be used."""


def notification_setting_for_item(notification_type: str, payload: dict[str, Any]) -> str | None:
    kind = str(payload.get("kind") or "").strip().lower()
    normalized_type = str(notification_type or "").strip()
    if normalized_type in {"system.market_open", "system.market_opened"} or kind in {"market_open", "market_opened"}:
        return "marketOpen"
    if normalized_type in {"system.market_close", "system.market_closed", "system.market_close_summary"} or kind in {"market_close", "market_closed", "market_close_summary"}:
        return "marketClose"
    if normalized_type == "system.volume_spike" or kind == "volume_spike":
        return "volumeSpike"
    if normalized_type == "system.rsi_band" or kind == "rsi_band":
        return "rsiBand"
    if normalized_type == "alert.price_cross":
        return "targetPrice"
    if normalized_type in {"alert.spike", "system.market_move"} or kind == "market_move":
        return "rapidMove"

    decision = _record(payload.get("decision"))
    event_type = str(decision.get("eventType") or payload.get("eventType") or "").strip().lower()
    if event_type == "volume_spike":
        return "volumeSpike"
    if event_type in {"price_surge", "price_drop"}:
        return "rapidMove"
    if event_type in {"extended_hours_move", "premarket_move", "after_hours_move"}:
        return "extendedHoursMove"
    if event_type in {"risk_anomaly_surge", "volatility_expansion"}:
        return "aiAnomaly"
    if event_type in {"social_issue", "controversy", "sentiment_crisis"}:
        return "socialIssue"
    return None


def notification_delivery_decision(
    notification_type: str,
    payload: dict[str, Any],
    preferences: dict[str, Any],
) -> tuple[bool, str]:
    settings = _record(preferences.get("settings"))
    thresholds = _record(preferences.get("thresholds"))
    kind = str(payload.get("kind") or "").strip().lower()
    if notification_type == "system.earnings_d1" or kind == "earnings_d1":
        return False, "event_excluded"
    if settings.get("master") is not True:
        return False, "master_disabled"

    # A user-created company rule is controlled by the global master and its own
    # bell/status. Reminder category switches must not silently disable it.
    if str(notification_type or "").startswith("alert.") and payload.get("alertId") is not None:
        return True, "allowed"

    setting = notification_setting_for_item(notification_type, payload)
    if setting is None and notification_type == "AGENT_ALERT":
        return False, "event_excluded"
    if setting is not None and settings.get(setting) is not True:
        return False, f"{setting}_disabled"

    symbol = notification_symbol(payload)
    company_overrides = _record(preferences.get("companyOverrides"))
    if symbol and company_overrides.get(symbol) is False:
        return False, "company_muted"

    if setting == "rapidMove":
        actual_change = _notification_metric(payload, "changePct", "changePercent", "percentChange")
        threshold = _number_or_none(thresholds.get("rapidMovePct"))
        if actual_change is None:
            return False, "rapid_move_metric_missing"
        if threshold is not None and abs(actual_change) < threshold:
            return False, "rapid_move_below_threshold"
    elif setting == "volumeSpike":
        actual_multiple = _notification_metric(payload, "multiplier", "volumeMultiple", "volumeRatio")
        threshold = _number_or_none(thresholds.get("volumeSpikeMultiple"))
        if actual_multiple is None:
            return False, "volume_spike_metric_missing"
        if threshold is not None and actual_multiple < threshold:
            return False, "volume_spike_below_threshold"
    elif setting == "extendedHoursMove":
        actual_change = _notification_metric(payload, "changePct", "changePercent", "percentChange")
        if actual_change is None:
            return False, "extended_hours_metric_missing"
        if abs(actual_change) < 5:
            return False, "extended_hours_below_threshold"

    return True, "allowed"


def notification_symbol(payload: dict[str, Any]) -> str:
    decision = _record(payload.get("decision"))
    value = str(payload.get("symbol") or decision.get("symbol") or "").strip().upper()
    if value in {"", "MARKET", "PORTFOLIO", "UNKNOWN", "ALERT"}:
        return ""
    return value


def _notification_metric(payload: dict[str, Any], *keys: str) -> float | None:
    decision = _record(payload.get("decision"))
    metric_sources = (
        payload,
        _record(payload.get("metrics")),
        decision,
        _record(decision.get("metrics")),
    )
    for source in metric_sources:
        for key in keys:
            value = _number_or_none(source.get(key))
            if value is not None:
                return value
    return None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RedisNotificationBroker:
    def __init__(self, redis_client: Any, channel_prefix: str | None = None) -> None:
        self.redis = redis_client
        self.channel_prefix = (channel_prefix or os.getenv("ALERT_NOTIFY_CHANNEL_PREFIX") or "notify").strip(":")

    @classmethod
    def from_env(cls) -> "RedisNotificationBroker":
        if redis is None:
            raise NotificationBrokerError("redis package is not installed")
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise NotificationBrokerError("REDIS_URL is required for notification broker")
        return cls(redis.from_url(redis_url, decode_responses=True))

    def publish_user(self, user_sub: str, payload: dict[str, Any]) -> None:
        self.redis.publish(self.channel(user_sub), _dumps(payload))

    async def listen_user(self, user_sub: str) -> AsyncIterator[dict[str, Any]]:
        pubsub = self.redis.pubsub()
        channel = self.channel(user_sub)
        pubsub.subscribe(channel)
        try:
            while True:
                message = await asyncio.to_thread(pubsub.get_message, ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    payload = _loads(message.get("data"))
                    if payload:
                        yield payload
                await asyncio.sleep(0.05)
        finally:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception:
                pass

    def channel(self, user_sub: str) -> str:
        return f"{self.channel_prefix}:{user_sub}"


class InMemoryNotificationBroker:
    def __init__(self) -> None:
        self.queues: dict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(asyncio.Queue)

    def publish_user(self, user_sub: str, payload: dict[str, Any]) -> None:
        self.queues[user_sub].put_nowait(dict(payload))

    async def listen_user(self, user_sub: str) -> AsyncIterator[dict[str, Any]]:
        queue = self.queues[user_sub]
        while True:
            yield await queue.get()


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return value if isinstance(value, dict) else {}


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
