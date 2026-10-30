import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth.dependencies import require_current_user  # noqa: E402
from app.auth.models import AuthenticatedUser  # noqa: E402
from app.main import create_app  # noqa: E402
from app.me.repository import (  # noqa: E402
    RECENT_SYMBOLS_KEY_PREFIX,
    InMemoryRecentSymbolsRepository,
    RedisRecentSymbolsRepository,
)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping))
        return self

    def zremrangebyrank(self, key, start, end):
        self.ops.append(("zremrangebyrank", key, start, end))
        return self

    def execute(self):
        for op in self.ops:
            getattr(self.redis, op[0])(*op[1:])


class FakeSortedSetRedis:
    """zadd / zremrangebyrank / zrevrange 만 흉내 냅니다."""

    def __init__(self):
        self.data = {}

    def pipeline(self):
        return FakePipeline(self)

    def zadd(self, key, mapping):
        self.data.setdefault(key, {}).update(mapping)

    def _ascending(self, key):
        return sorted(self.data.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))

    def zremrangebyrank(self, key, start, end):
        items = self._ascending(key)
        end = len(items) + end if end < 0 else end
        if end < start:
            return
        for member, _ in items[start:end + 1]:
            del self.data[key][member]

    def zrevrange(self, key, start, end, withscores=False):
        items = list(reversed(self._ascending(key)))[start:end + 1]
        return [(m, float(s)) for m, s in items] if withscores else [m for m, _ in items]


class RecentSymbolsRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.app = create_app()
        self.app.state.recent_symbols_repository = InMemoryRecentSymbolsRepository()
        self.app.state.recent_symbols_now_provider = Clock()
        self.client = TestClient(self.app)

    def test_empty_by_default(self):
        response = self.client.get("/api/me/recent-symbols")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_post_then_list_newest_first_with_korean_name(self):
        self.assertEqual(self.client.post("/api/me/recent-symbols", json={"symbol": "nvda"}).status_code, 204)
        self.assertEqual(self.client.post("/api/me/recent-symbols", json={"symbol": "AAPL"}).status_code, 204)

        response = self.client.get("/api/me/recent-symbols")

        self.assertEqual(response.json(), {"items": [
            {"symbol": "AAPL", "nameKo": "애플", "viewedAt": "2026-10-30T12:00:02.000Z"},
            {"symbol": "NVDA", "nameKo": "엔비디아", "viewedAt": "2026-10-30T12:00:01.000Z"},
        ]})

    def test_revisiting_moves_symbol_to_front_without_duplicates(self):
        for symbol in ("NVDA", "AAPL", "MSFT", "NVDA"):
            self.client.post("/api/me/recent-symbols", json={"symbol": symbol})

        symbols = [item["symbol"] for item in self.client.get("/api/me/recent-symbols").json()["items"]]
        self.assertEqual(symbols, ["NVDA", "MSFT", "AAPL"])

    def test_keeps_only_latest_50(self):
        for index in range(55):
            self.client.post("/api/me/recent-symbols", json={"symbol": f"S{index}"})

        items = self.client.get("/api/me/recent-symbols").json()["items"]
        self.assertEqual(len(items), 50)
        self.assertEqual(items[0]["symbol"], "S54")
        self.assertEqual(items[-1]["symbol"], "S5")

    def test_rejects_invalid_symbol(self):
        for bad in ("", "NV DA", "A" * 17, "삼성", "$NVDA"):
            response = self.client.post("/api/me/recent-symbols", json={"symbol": bad})
            self.assertEqual(response.status_code, 422, bad)
        self.assertEqual(
            self.client.post("/api/me/recent-symbols", json={"symbol": "x y"}).json(),
            {"detail": {"code": "invalid_symbol"}},
        )
        self.assertEqual(self.client.post("/api/me/recent-symbols", json={"symbol": "brk.b"}).status_code, 204)

    def test_lists_are_per_user(self):
        users = {"current": AuthenticatedUser("sub-a", "a@x", True, app_user_id="user-a")}
        self.app.dependency_overrides[require_current_user] = lambda: users["current"]
        self.client.post("/api/me/recent-symbols", json={"symbol": "NVDA"})
        users["current"] = AuthenticatedUser("sub-b", "b@x", True, app_user_id=None)
        self.client.post("/api/me/recent-symbols", json={"symbol": "TSLA"})

        self.assertEqual([i["symbol"] for i in self.client.get("/api/me/recent-symbols").json()["items"]], ["TSLA"])
        users["current"] = AuthenticatedUser("sub-a", "a@x", True, app_user_id="user-a")
        self.assertEqual([i["symbol"] for i in self.client.get("/api/me/recent-symbols").json()["items"]], ["NVDA"])
        self.assertEqual(set(self.app.state.recent_symbols_repository._items), {"user-a", "sub-b"})

    def test_requires_login_when_auth_enabled(self):
        with mock.patch.dict(os.environ, {
            "AUTH_ENABLED": "true",
            "AUTH_SESSION_SECRET": "test-session-secret",
            "AUTH_REDIS_URL": "",
            "AUTH_REDIS_KEY_PREFIX": "gops:test-auth",
        }, clear=False):
            client = TestClient(create_app())
            read = client.get("/api/me/recent-symbols")
            write = client.post("/api/me/recent-symbols", json={"symbol": "NVDA"})
        self.assertEqual(read.status_code, 401)
        self.assertEqual(write.status_code, 401)


class RedisRecentSymbolsRepositoryTest(unittest.TestCase):
    def test_uses_sorted_set_key_and_trims_to_limit(self):
        redis = FakeSortedSetRedis()
        repository = RedisRecentSymbolsRepository(redis, limit=3)
        start = datetime(2026, 10, 30, tzinfo=timezone.utc)
        for offset, symbol in enumerate(["A", "B", "C", "D", "B"]):
            repository.add("user-1", symbol, start + timedelta(seconds=offset))

        key = RECENT_SYMBOLS_KEY_PREFIX + "user-1"
        self.assertEqual(key, "gops:me:recent-symbols:v1:user-1")
        self.assertEqual(set(redis.data[key]), {"B", "C", "D"})
        rows = repository.list("user-1")
        self.assertEqual([symbol for symbol, _ in rows], ["B", "D", "C"])
        self.assertEqual(rows[0][1], start + timedelta(seconds=4))


if __name__ == "__main__":
    unittest.main()
