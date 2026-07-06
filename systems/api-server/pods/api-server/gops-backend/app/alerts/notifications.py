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
