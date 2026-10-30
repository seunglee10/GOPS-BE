"""최근 본 종목 저장소(D18).

Redis sorted set 하나에 사용자별로 저장합니다. member=종목, score=본 시각(epoch ms).
같은 종목을 다시 보면 score 만 바뀌어 맨 앞으로 오고, 50개를 넘으면 오래된 것부터 지웁니다.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Protocol

RECENT_SYMBOLS_KEY_PREFIX = "gops:me:recent-symbols:v1:"
RECENT_SYMBOLS_LIMIT = 50


class RecentSymbolsRepository(Protocol):
    def add(self, user_key: str, symbol: str, viewed_at: datetime) -> None: ...

    def list(self, user_key: str, limit: int = RECENT_SYMBOLS_LIMIT) -> list[tuple[str, datetime]]: ...


def _to_ms(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _from_ms(value: float) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


class RedisRecentSymbolsRepository:
    def __init__(self, redis_client: Any, *, limit: int = RECENT_SYMBOLS_LIMIT) -> None:
        self._redis = redis_client
        self._limit = limit

    @classmethod
    def from_env(cls) -> "RedisRecentSymbolsRepository":
        import redis

        redis_url = os.getenv("REDIS_URL") or "redis://localhost:6379/0"
        return cls(redis.from_url(redis_url, decode_responses=True))

    def add(self, user_key: str, symbol: str, viewed_at: datetime) -> None:
        key = RECENT_SYMBOLS_KEY_PREFIX + user_key
        pipe = self._redis.pipeline()
        pipe.zadd(key, {symbol: _to_ms(viewed_at)})
        # 점수 오름차순 0..-(limit+1) 은 최신 limit 개를 뺀 나머지다.
        pipe.zremrangebyrank(key, 0, -(self._limit + 1))
        pipe.execute()

    def list(self, user_key: str, limit: int = RECENT_SYMBOLS_LIMIT) -> list[tuple[str, datetime]]:
        rows = self._redis.zrevrange(RECENT_SYMBOLS_KEY_PREFIX + user_key, 0, max(0, limit - 1), withscores=True)
        return [(str(member), _from_ms(score)) for member, score in rows or []]


class InMemoryRecentSymbolsRepository:
    def __init__(self, *, limit: int = RECENT_SYMBOLS_LIMIT) -> None:
        self._limit = limit
        self._items: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def add(self, user_key: str, symbol: str, viewed_at: datetime) -> None:
        with self._lock:
            items = self._items.setdefault(user_key, {})
            items[symbol] = _to_ms(viewed_at)
            if len(items) > self._limit:
                for stale, _ in sorted(items.items(), key=lambda kv: (kv[1], kv[0]))[: len(items) - self._limit]:
                    del items[stale]

    def list(self, user_key: str, limit: int = RECENT_SYMBOLS_LIMIT) -> list[tuple[str, datetime]]:
        with self._lock:
            items = sorted(self._items.get(user_key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
        return [(symbol, _from_ms(score)) for symbol, score in items[:limit]]
