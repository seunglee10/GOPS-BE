from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.config import read_dotenv_value


FAST_SUGGESTION_QUERY = "거래대금이 강하고 추세가 이어지는 종목"
SUGGESTION_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
SUGGESTION_CACHE_KEY_PREFIX = "gops:recommendations:score-profile-suggestion:v1"
SUGGESTION_SCHEMA_VERSION = "recommendation-score-suggestion.v1"


class RedisScoreProfileSuggestionCache:
    def __init__(self, redis_client: Any, *, ttl_seconds: int = SUGGESTION_CACHE_TTL_SECONDS) -> None:
        self.redis = redis_client
        self.ttl_seconds = int(ttl_seconds)

    def get(self, user_sub: str, query: str) -> dict[str, Any] | None:
        if not is_fast_suggestion_query(query):
            return None
        payload = self.redis.get(self._key(user_sub))
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if not payload:
            return None
        parsed = json.loads(str(payload))
        if not isinstance(parsed, dict) or parsed.get("schemaVersion") != SUGGESTION_SCHEMA_VERSION:
            return None
        return parsed

    def put(self, user_sub: str, query: str, suggestion: dict[str, Any]) -> None:
        if not is_fast_suggestion_query(query):
            return
        self.redis.set(
            self._key(user_sub),
            json.dumps(suggestion, ensure_ascii=False, separators=(",", ":")),
            ex=self.ttl_seconds,
        )

    def _key(self, user_sub: str) -> str:
        user_digest = hashlib.sha256(str(user_sub).encode("utf-8")).hexdigest()[:24]
        return f"{SUGGESTION_CACHE_KEY_PREFIX}:{user_digest}:strong-dollar-volume-trend"


def is_fast_suggestion_query(query: str) -> bool:
    return " ".join(str(query or "").split()) == FAST_SUGGESTION_QUERY


def cached_score_profile_suggestion(app: Any, user_sub: str, query: str) -> dict[str, Any] | None:
    if not is_fast_suggestion_query(query):
        return None
    cache = _cache_from_app(app)
    if cache is None:
        return None
    try:
        return cache.get(user_sub, query)
    except Exception:
        return None


def cache_score_profile_suggestion(app: Any, user_sub: str, query: str, suggestion: dict[str, Any]) -> None:
    if not is_fast_suggestion_query(query):
        return
    cache = _cache_from_app(app)
    if cache is None:
        return
    try:
        cache.put(user_sub, query, suggestion)
    except Exception:
        return


def _cache_from_app(app: Any) -> Any | None:
    existing = getattr(app.state, "recommendation_profile_suggestion_cache", None)
    if existing is not None:
        return existing
    if getattr(app.state, "recommendation_profile_suggestion_cache_initialized", False):
        return None
    app.state.recommendation_profile_suggestion_cache_initialized = True
    redis_url = read_dotenv_value("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis

        redis_client = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
    except Exception:
        return None
    cache = RedisScoreProfileSuggestionCache(redis_client)
    app.state.recommendation_profile_suggestion_cache = cache
    return cache
