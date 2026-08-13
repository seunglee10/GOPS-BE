from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.recommendations.suggestion_cache import (  # noqa: E402
    FAST_SUGGESTION_QUERY,
    SUGGESTION_CACHE_TTL_SECONDS,
    RedisScoreProfileSuggestionCache,
    is_fast_suggestion_query,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.expirations[key] = ex


def test_fast_suggestion_cache_is_user_scoped_and_expires_after_thirty_days() -> None:
    redis = FakeRedis()
    cache = RedisScoreProfileSuggestionCache(redis)
    suggestion = {"schemaVersion": "recommendation-score-suggestion.v1", "name": "거래 참여 추세"}

    cache.put("user-a", FAST_SUGGESTION_QUERY, suggestion)

    assert cache.get("user-a", FAST_SUGGESTION_QUERY) == suggestion
    assert cache.get("user-b", FAST_SUGGESTION_QUERY) is None
    assert list(redis.expirations.values()) == [SUGGESTION_CACHE_TTL_SECONDS]
    assert SUGGESTION_CACHE_TTL_SECONDS == 2_592_000


def test_only_the_named_quick_query_is_cacheable() -> None:
    redis = FakeRedis()
    cache = RedisScoreProfileSuggestionCache(redis)

    assert is_fast_suggestion_query(FAST_SUGGESTION_QUERY)
    assert not is_fast_suggestion_query("돌파 후 VWAP을 지키는 종목")

    cache.put("user-a", "돌파 후 VWAP을 지키는 종목", {"name": "uncached"})

    assert redis.values == {}


def test_cache_rejects_an_unknown_suggestion_schema() -> None:
    redis = FakeRedis()
    cache = RedisScoreProfileSuggestionCache(redis)

    cache.put("user-a", FAST_SUGGESTION_QUERY, {"schemaVersion": "unknown", "name": "stale"})

    assert cache.get("user-a", FAST_SUGGESTION_QUERY) is None
