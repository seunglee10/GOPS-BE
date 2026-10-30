"""에이전트 대화 목록 (D16).

사용자별 Redis 키 두 종류에 저장한다.
- `gops:agent:threads:v1:{user}`: sorted set. member=threadId, score=updatedAt(ms). 최근 50개만 남긴다.
- `gops:agent:threads:v1:{user}:t:{threadId}`: hash. title·symbol·updatedAt, messages(JSON 배열:
  [{analysisId, question}], 최근 100개).
목록에서 밀려난 대화는 hash 도 지운다.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Protocol

THREADS_KEY_PREFIX = "gops:agent:threads:v1:"
THREADS_LIMIT = 50
THREAD_MESSAGES_LIMIT = 100
THREAD_TITLE_LIMIT = 40


class AgentThreadStore(Protocol):
    def record(
        self, user_key: str, thread_id: str, *, question: str, symbol: str | None, analysis_id: str, at: datetime,
    ) -> None: ...

    def list(self, user_key: str) -> list[dict[str, Any]]: ...

    def get(self, user_key: str, thread_id: str) -> dict[str, Any] | None: ...


def thread_title(question: str) -> str:
    text = " ".join(str(question or "").split())
    return text if len(text) <= THREAD_TITLE_LIMIT else text[:THREAD_TITLE_LIMIT - 1].rstrip() + "…"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _append_message(messages: list[dict[str, Any]], analysis_id: str, question: str) -> list[dict[str, Any]]:
    if any(item.get("analysisId") == analysis_id for item in messages):
        return messages
    return [*messages, {"analysisId": analysis_id, "question": question}][-THREAD_MESSAGES_LIMIT:]


class RedisAgentThreadStore:
    def __init__(self, redis_client: Any, *, limit: int = THREADS_LIMIT) -> None:
        self._redis = redis_client
        self._limit = limit

    @classmethod
    def from_env(cls) -> "RedisAgentThreadStore":
        import redis

        return cls(redis.from_url(os.getenv("REDIS_URL") or "redis://localhost:6379/0", decode_responses=True))

    def _index(self, user_key: str) -> str:
        return THREADS_KEY_PREFIX + user_key

    def _thread(self, user_key: str, thread_id: str) -> str:
        return f"{THREADS_KEY_PREFIX}{user_key}:t:{thread_id}"

    def record(
        self, user_key: str, thread_id: str, *, question: str, symbol: str | None, analysis_id: str, at: datetime,
    ) -> None:
        key = self._thread(user_key, thread_id)
        current = self._redis.hgetall(key) or {}
        messages = _decode_messages(current.get("messages"))
        mapping = {
            "title": current.get("title") or thread_title(question),
            "symbol": current.get("symbol") or (symbol or ""),
            "updatedAt": _iso(at),
            "messages": json.dumps(_append_message(messages, analysis_id, question), ensure_ascii=False),
        }
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.zadd(self._index(user_key), {thread_id: _ms(at)})
        pipe.execute()
        stale = self._redis.zrange(self._index(user_key), 0, -(self._limit + 1)) or []
        if stale:
            pipe = self._redis.pipeline()
            pipe.zrem(self._index(user_key), *stale)
            for stale_id in stale:
                pipe.delete(self._thread(user_key, str(stale_id)))
            pipe.execute()

    def list(self, user_key: str) -> list[dict[str, Any]]:
        thread_ids = self._redis.zrevrange(self._index(user_key), 0, self._limit - 1) or []
        items = []
        for thread_id in thread_ids:
            row = self._redis.hgetall(self._thread(user_key, str(thread_id))) or {}
            if row:
                items.append(_summary(str(thread_id), row))
        return items

    def get(self, user_key: str, thread_id: str) -> dict[str, Any] | None:
        row = self._redis.hgetall(self._thread(user_key, thread_id)) or {}
        if not row:
            return None
        return {**_summary(thread_id, row), "messages": _decode_messages(row.get("messages"))}


class InMemoryAgentThreadStore:
    def __init__(self, *, limit: int = THREADS_LIMIT) -> None:
        self._limit = limit
        self._threads: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def record(
        self, user_key: str, thread_id: str, *, question: str, symbol: str | None, analysis_id: str, at: datetime,
    ) -> None:
        with self._lock:
            threads = self._threads.setdefault(user_key, {})
            current = threads.get(thread_id) or {"title": thread_title(question), "symbol": symbol or "", "messages": []}
            threads[thread_id] = {
                "title": current["title"],
                "symbol": current["symbol"] or (symbol or ""),
                "updatedAt": _iso(at),
                "updatedMs": _ms(at),
                "messages": _append_message(current["messages"], analysis_id, question),
            }
            ordered = sorted(threads.items(), key=lambda item: (item[1]["updatedMs"], item[0]), reverse=True)
            for stale_id, _row in ordered[self._limit:]:
                del threads[stale_id]

    def list(self, user_key: str) -> list[dict[str, Any]]:
        with self._lock:
            threads = dict(self._threads.get(user_key, {}))
        ordered = sorted(threads.items(), key=lambda item: (item[1]["updatedMs"], item[0]), reverse=True)
        return [_summary(thread_id, row) for thread_id, row in ordered]

    def get(self, user_key: str, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = (self._threads.get(user_key) or {}).get(thread_id)
            if row is None:
                return None
            return {**_summary(thread_id, row), "messages": [dict(item) for item in row["messages"]]}


def _summary(thread_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "title": row.get("title") or "",
        "symbol": row.get("symbol") or None,
        "updatedAt": row.get("updatedAt"),
    }


def _decode_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
