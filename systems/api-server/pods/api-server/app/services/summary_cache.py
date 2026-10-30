"""AI 요약 캐시(D14 종목 요약, D15 시장 요약) 읽기.

값은 agent-orchestration 의 stock-summary-refresh 잡이 Redis 에 써 둔다.
API 는 캐시만 읽고 생성하지 않는다. 테스트는 app.state.summary_cache_redis 에 가짜 클라이언트를 넣는다.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from app.core.config import read_dotenv_value

_redis_client: Any | None = None
_redis_initialized = False


def summary_cache_redis(request: Request) -> Any | None:
    override = getattr(request.app.state, "summary_cache_redis", None)
    if override is not None:
        return override
    global _redis_client, _redis_initialized
    if not _redis_initialized:
        _redis_initialized = True
        redis_url = read_dotenv_value("REDIS_URL")
        if redis_url:
            try:
                import redis

                _redis_client = redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                )
            except Exception:
                _redis_client = None
    return _redis_client


def read_cached_json(redis_client: Any | None, key: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
