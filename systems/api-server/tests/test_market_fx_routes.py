import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.market_data.fx import service as fx_service

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def delete(self, key):
        self.values.pop(key, None)
        return 1


class FakeRedisProvider:
    def __init__(self):
        self.redis = FakeRedis()


class FakeProvider:
    def __init__(self):
        self.redis_provider = FakeRedisProvider()


KRW_ROWS = {
    "KRW=X": [
        {"timestamp": "2026-10-30T08:55:00Z", "Close": 1380.1},
        {"timestamp": "2026-10-30T09:00:00Z", "Close": 1382.5},
    ]
}


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class MarketFxRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.calls = []
        self.fail_upstream = False
        self.provider = FakeProvider()
        self.app = create_app()
        self.app.state.fx_service = fx_service.FxRateService(provider=self.provider, fetcher=self.fetcher)
        self.client = TestClient(self.app)

    def fetcher(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_upstream:
            raise TimeoutError("yahoo timeout")
        return KRW_ROWS

    def test_returns_usdkrw_rate_from_latest_close(self):
        response = self.client.get("/api/market/fx?pair=USDKRW")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"pair": "USDKRW", "rate": 1382.5, "asOf": "2026-10-30T09:00:00Z", "source": "yahoo-finance"},
        )
        self.assertIsInstance(response.json()["rate"], float)
        self.assertEqual(self.calls[0]["symbols"], ["KRW=X"])

    def test_default_pair_is_usdkrw_and_lowercase_or_slash_is_accepted(self):
        self.assertEqual(self.client.get("/api/market/fx").json()["pair"], "USDKRW")
        self.assertEqual(self.client.get("/api/market/fx?pair=usd/krw").json()["pair"], "USDKRW")

    def test_second_call_within_ten_minutes_uses_cache(self):
        first = self.client.get("/api/market/fx?pair=USDKRW").json()
        second = self.client.get("/api/market/fx?pair=USDKRW").json()
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        redis = self.provider.redis_provider.redis
        self.assertEqual(redis.expirations[fx_service.fx_cache_key("USDKRW")], fx_service.DEFAULT_FX_CACHE_TTL_SECONDS)
        self.assertEqual(fx_service.DEFAULT_FX_CACHE_TTL_SECONDS, 600)

    def test_memory_cache_covers_missing_redis(self):
        class NoRedisProvider:
            redis_provider = None

        self.app.state.fx_service = fx_service.FxRateService(provider=NoRedisProvider(), fetcher=self.fetcher)
        self.client.get("/api/market/fx?pair=USDKRW")
        self.client.get("/api/market/fx?pair=USDKRW")
        self.assertEqual(len(self.calls), 1)

    def test_unknown_pair_returns_400(self):
        response = self.client.get("/api/market/fx?pair=EURUSD")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "unsupported_pair")
        self.assertEqual(self.calls, [])

    def test_upstream_failure_without_cache_returns_503(self):
        self.fail_upstream = True
        response = self.client.get("/api/market/fx?pair=USDKRW")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "fx_unavailable")

    def test_upstream_failure_with_cache_still_serves_rate(self):
        self.client.get("/api/market/fx?pair=USDKRW")
        self.fail_upstream = True
        response = self.client.get("/api/market/fx?pair=USDKRW")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rate"], 1382.5)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
