from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable

from ..contracts import EvidenceItem


DEFAULT_GRAPH_PATH_CACHE_PREFIX = "gops:agent:graph-path"


class GraphPathCache:
    def get(self, *, symbols: tuple[str, ...], intent_themes: tuple[str, ...], limit: int) -> list[EvidenceItem] | None:
        raise NotImplementedError

    def set(
        self,
        *,
        symbols: tuple[str, ...],
        intent_themes: tuple[str, ...],
        limit: int,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        raise NotImplementedError

    def key(self, *, symbols: tuple[str, ...], intent_themes: tuple[str, ...], limit: int) -> str:
        return graph_path_cache_key(
            symbols=symbols,
            intent_themes=intent_themes,
            limit=limit,
            prefix=os.getenv("AGENT_GRAPH_PATH_CACHE_KEY_PREFIX", DEFAULT_GRAPH_PATH_CACHE_PREFIX),
        )


class NullGraphPathCache(GraphPathCache):
    def get(self, *, symbols: tuple[str, ...], intent_themes: tuple[str, ...], limit: int) -> list[EvidenceItem] | None:
        return None

    def set(
        self,
        *,
        symbols: tuple[str, ...],
        intent_themes: tuple[str, ...],
        limit: int,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        return None


@dataclass
class GraphPathCacheEntry:
    expires_at: float
    payload: str


class MemoryGraphPathCache(GraphPathCache):
    def __init__(self, *, now_fn: Callable[[], float] | None = None):
        self._items: dict[str, GraphPathCacheEntry] = {}
        self._now_fn = now_fn or time.time

    def get(self, *, symbols: tuple[str, ...], intent_themes: tuple[str, ...], limit: int) -> list[EvidenceItem] | None:
        key = self.key(symbols=symbols, intent_themes=intent_themes, limit=limit)
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
        symbols: tuple[str, ...],
        intent_themes: tuple[str, ...],
        limit: int,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        key = self.key(symbols=symbols, intent_themes=intent_themes, limit=limit)
        self._items[key] = GraphPathCacheEntry(
            expires_at=self._now_fn() + ttl_seconds,
            payload=serialize_evidence_items(items),
        )


class RedisGraphPathCache(GraphPathCache):
    def __init__(self, redis_client=None, *, redis_url: str | None = None):
        if redis_client is not None:
            self.redis = redis_client
        else:
            import redis

            self.redis = redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

    def get(self, *, symbols: tuple[str, ...], intent_themes: tuple[str, ...], limit: int) -> list[EvidenceItem] | None:
        try:
            key = self.key(symbols=symbols, intent_themes=intent_themes, limit=limit)
            payload = self.redis.get(key)
            return deserialize_evidence_items(payload) if payload else None
        except Exception:
            return None

    def set(
        self,
        *,
        symbols: tuple[str, ...],
        intent_themes: tuple[str, ...],
        limit: int,
        items: list[EvidenceItem],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        try:
            key = self.key(symbols=symbols, intent_themes=intent_themes, limit=limit)
            self.redis.setex(key, ttl_seconds, serialize_evidence_items(items))
        except Exception:
            return None


def build_graph_path_cache_from_env() -> GraphPathCache:
    backend = os.getenv("AGENT_GRAPH_PATH_CACHE_BACKEND", "auto").strip().lower()
    if backend in {"off", "none", "null", "false", "0"}:
        return NullGraphPathCache()
    if backend == "memory":
        return MemoryGraphPathCache()
    if backend == "redis" or (backend == "auto" and os.getenv("REDIS_URL")):
        try:
            return RedisGraphPathCache()
        except Exception:
            if backend == "redis":
                return NullGraphPathCache()
    return MemoryGraphPathCache()


def graph_path_cache_key(
    *,
    symbols: tuple[str, ...],
    intent_themes: tuple[str, ...],
    limit: int,
    prefix: str = DEFAULT_GRAPH_PATH_CACHE_PREFIX,
) -> str:
    normalized_symbols = ",".join(sorted(str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()))
    normalized_themes = ",".join(sorted(str(theme or "").strip() for theme in intent_themes if str(theme or "").strip()))
    return f"{prefix}:evidence:v1:{normalized_symbols or 'UNKNOWN'}:{normalized_themes or 'NONE'}:{int(limit)}"


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
