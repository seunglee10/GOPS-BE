from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable

from ..contracts import EvidenceItem


DEFAULT_NEWS_CACHE_PREFIX = "gops:agent:news"


class NewsEvidenceCache:
    def get(self, *, symbol: str, limit: int, days: int, fallback_enabled: bool) -> list[EvidenceItem] | None:
        raise NotImplementedError

    def set(
        self,
        *,
        symbol: str,
        limit: int,
        days: int,
        fallback_enabled: bool,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        raise NotImplementedError

    def key(self, *, symbol: str, limit: int, days: int, fallback_enabled: bool) -> str:
        return news_cache_key(
            symbol=symbol,
            limit=limit,
            days=days,
            fallback_enabled=fallback_enabled,
            prefix=os.getenv("AGENT_NEWS_CACHE_KEY_PREFIX", DEFAULT_NEWS_CACHE_PREFIX),
        )


class NullNewsEvidenceCache(NewsEvidenceCache):
    def get(self, *, symbol: str, limit: int, days: int, fallback_enabled: bool) -> list[EvidenceItem] | None:
        return None

    def set(
        self,
        *,
        symbol: str,
        limit: int,
        days: int,
        fallback_enabled: bool,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        return None


@dataclass
class MemoryCacheEntry:
    expires_at: float
    payload: str


class MemoryNewsEvidenceCache(NewsEvidenceCache):
    def __init__(self, *, now_fn: Callable[[], float] | None = None):
        self._items: dict[str, MemoryCacheEntry] = {}
        self._now_fn = now_fn or time.time

    def get(self, *, symbol: str, limit: int, days: int, fallback_enabled: bool) -> list[EvidenceItem] | None:
        key = self.key(symbol=symbol, limit=limit, days=days, fallback_enabled=fallback_enabled)
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._now_fn():
            self._items.pop(key, None)
            return None
        try:
            return deserialize_evidence_items(entry.payload)
        except Exception:
            self._items.pop(key, None)
            return None

    def set(
        self,
        *,
        symbol: str,
        limit: int,
        days: int,
        fallback_enabled: bool,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        key = self.key(symbol=symbol, limit=limit, days=days, fallback_enabled=fallback_enabled)
        self._items[key] = MemoryCacheEntry(
            expires_at=self._now_fn() + ttl_seconds,
            payload=serialize_evidence_items(items),
        )


class RedisNewsEvidenceCache(NewsEvidenceCache):
    def __init__(self, redis_client=None, *, redis_url: str | None = None):
        if redis_client is not None:
            self.redis = redis_client
        else:
            import redis

            self.redis = redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

    def get(self, *, symbol: str, limit: int, days: int, fallback_enabled: bool) -> list[EvidenceItem] | None:
        try:
            key = self.key(symbol=symbol, limit=limit, days=days, fallback_enabled=fallback_enabled)
            payload = self.redis.get(key)
            return deserialize_evidence_items(payload) if payload else None
        except Exception:
            return None

    def set(
        self,
        *,
        symbol: str,
        limit: int,
        days: int,
        fallback_enabled: bool,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        try:
            key = self.key(symbol=symbol, limit=limit, days=days, fallback_enabled=fallback_enabled)
            self.redis.setex(key, ttl_seconds, serialize_evidence_items(items))
        except Exception:
            return None


def build_news_cache_from_env() -> NewsEvidenceCache:
    backend = os.getenv("AGENT_NEWS_CACHE_BACKEND", "auto").strip().lower()
    if backend in {"off", "none", "null", "false", "0"}:
        return NullNewsEvidenceCache()
    if backend == "memory":
        return MemoryNewsEvidenceCache()
    if backend == "redis" or (backend == "auto" and os.getenv("REDIS_URL")):
        try:
            return RedisNewsEvidenceCache()
        except Exception:
            if backend == "redis":
                return NullNewsEvidenceCache()
    return MemoryNewsEvidenceCache()


def news_cache_key(*, symbol: str, limit: int, days: int, fallback_enabled: bool, prefix: str = DEFAULT_NEWS_CACHE_PREFIX) -> str:
    normalized_symbol = str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
    fallback_value = "true" if fallback_enabled else "false"
    return f"{prefix}:evidence:v1:{normalized_symbol}:{int(limit)}:{int(days)}:{fallback_value}"


def serialize_evidence_items(items: list[EvidenceItem]) -> str:
    return json.dumps([item.to_dict() for item in items], ensure_ascii=False, separators=(",", ":"))


def deserialize_evidence_items(payload: str) -> list[EvidenceItem] | None:
    decoded = json.loads(payload)
    if not isinstance(decoded, list):
        return None
    return [
        EvidenceItem(**item)
        for item in decoded
        if isinstance(item, dict)
    ]
