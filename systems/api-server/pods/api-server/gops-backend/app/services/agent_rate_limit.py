from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from fastapi import HTTPException, status

from app.core.config import read_dotenv_value


def enforce_agent_rate_limit(app: Any, user_id: str, *, now: float | None = None) -> None:
    if not bool_config("AGENT_RATE_LIMIT_ENABLED", True):
        return
    limit = positive_int_config("AGENT_RATE_LIMIT_REQUESTS", 10)
    window_seconds = positive_int_config("AGENT_RATE_LIMIT_WINDOW_SECONDS", 60)
    current = time.time() if now is None else now
    window = int(current // window_seconds)
    user_hash = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:24]
    key = f"gops:agent:rate-limit:v1:{user_hash}:{window}"
    client = rate_limit_redis_from_app(app)
    try:
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, window_seconds)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent request admission is temporarily unavailable.",
        ) from exc
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Agent request rate limit exceeded.",
            headers={"Retry-After": str(window_seconds)},
        )


def rate_limit_redis_from_app(app: Any):
    existing = getattr(app.state, "agent_rate_limit_redis", None)
    if existing is not None:
        return existing
    redis_url = read_dotenv_value("REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for agent rate limiting")
    import redis

    client = redis.from_url(redis_url, decode_responses=True)
    app.state.agent_rate_limit_redis = client
    return client


def bool_config(name: str, default: bool) -> bool:
    value = read_dotenv_value(name)
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def positive_int_config(name: str, default: int) -> int:
    value = read_dotenv_value(name)
    if value is None:
        value = os.getenv(name)
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
