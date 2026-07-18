import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
for path in (str(BACKEND), str(MARKET_SHARED)):
    if path not in sys.path:
        sys.path.insert(0, path)

sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

from app.market_data.heatmap.service import MarketHeatmapService  # noqa: E402
from app.market_data.heatmap.worker import warm_once  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)

    def eval(self, _script, _key_count, key, expected_value):
        if self.values.get(key) != expected_value:
            return 0
        self.delete(key)
        return 1


class FakeProvider:
    def __init__(self):
        self.redis_provider = types.SimpleNamespace(redis=FakeRedis())


def test_api_snapshot_returns_stale_without_rebuilding():
    provider = FakeProvider()
    provider.redis_provider.redis.values["gops:market:on-demand:v1:heatmap:sp500:last"] = json.dumps({
        "source": "market-heatmap-projection",
        "items": [{"symbol": "AAPL", "sector": "Information Technology"}],
    })
    adapter = Mock()
    service = MarketHeatmapService(provider=provider, fundamentals_adapter=adapter)

    payload = service.snapshot("sp500", allow_rebuild=False)

    assert payload["source"] == "market-heatmap-projection"
    assert payload["cacheStatus"] == "stale"
    assert payload["items"][0]["symbol"] == "AAPL"
    adapter.latest_for_symbols.assert_not_called()


def test_api_snapshot_marks_seed_fallback_without_rebuilding():
    provider = FakeProvider()
    adapter = Mock()
    service = MarketHeatmapService(provider=provider, fundamentals_adapter=adapter)

    payload = service.snapshot("sp500", allow_rebuild=False)

    assert payload["source"] == "market-heatmap-seed"
    assert payload["cacheStatus"] == "seed"
    adapter.latest_for_symbols.assert_not_called()


def test_projection_worker_uses_lock_and_forces_a_fresh_projection():
    provider = FakeProvider()
    service = Mock()
    service._redis.return_value = provider.redis_provider.redis
    service.rebuild.return_value = {"items": []}

    assert warm_once(service, "sp500") is True
    service.rebuild.assert_called_once_with("sp500")
    service.snapshot.assert_not_called()
    assert "build-lock" not in provider.redis_provider.redis.values

    provider.redis_provider.redis.values["gops:market:on-demand:v1:heatmap:sp500:build-lock"] = "1"
    service.reset_mock()
    assert warm_once(service, "sp500") is False
    service.rebuild.assert_not_called()


def test_projection_worker_does_not_delete_a_new_owners_lock():
    provider = FakeProvider()
    redis_client = provider.redis_provider.redis
    lock_key = "gops:market:on-demand:v1:heatmap:sp500:build-lock"
    service = Mock()
    service._redis.return_value = redis_client

    def replace_expired_lock(_universe):
        redis_client.values[lock_key] = "new-owner"
        return {"items": []}

    service.rebuild.side_effect = replace_expired_lock

    assert warm_once(service, "sp500") is True
    assert redis_client.values[lock_key] == "new-owner"
