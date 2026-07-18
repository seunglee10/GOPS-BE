import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
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
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        with self._lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def delete(self, key):
        with self._lock:
            self.values.pop(key, None)

    def eval(self, _script, _key_count, key, expected_value):
        with self._lock:
            if self.values.get(key) != expected_value:
                return 0
            self.values.pop(key, None)
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


def test_ten_concurrent_warmers_build_the_projection_once():
    provider = FakeProvider()
    redis_client = provider.redis_provider.redis
    barrier = threading.Barrier(10)
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    class BlockingService:
        def __init__(self):
            self.rebuild_count = 0
            self._count_lock = threading.Lock()

        def _redis(self):
            return redis_client

        def rebuild(self, _universe):
            with self._count_lock:
                self.rebuild_count += 1
            refresh_started.set()
            assert release_refresh.wait(timeout=2)
            return {"items": []}

    service = BlockingService()

    def run_warmer():
        barrier.wait(timeout=2)
        return warm_once(service, "sp500")

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(run_warmer) for _ in range(10)]
        assert refresh_started.wait(timeout=2)
        release_refresh.set()
        results = [future.result(timeout=2) for future in futures]

    assert sum(results) == 1
    assert service.rebuild_count == 1


def test_projection_worker_is_rendered_and_redeployed_with_the_backend_image():
    deployment = (ROOT / "infra" / "k8s" / "base" / "app" / "deployment-heatmap-projection-worker.yaml").read_text(encoding="utf-8")
    kustomization = (ROOT / "infra" / "k8s" / "base" / "app" / "kustomization.yaml").read_text(encoding="utf-8")
    image_helpers = (ROOT / "scripts" / "aws" / "lib-gops-images.sh").read_text(encoding="utf-8")
    backend_env = (ROOT / "systems" / "api-server" / ".env.example").read_text(encoding="utf-8")

    assert "name: gops-heatmap-projection-worker" in deployment
    assert "deployment-heatmap-projection-worker.yaml" in kustomization
    assert "gops-heatmap-projection-worker" in image_helpers
    assert "HEATMAP_CACHE_TTL_SECONDS=70" in backend_env
