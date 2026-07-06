import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
if str(MARKET_SHARED) not in sys.path:
    sys.path.insert(0, str(MARKET_SHARED))

from alfaka.serving.chart_derived_data import (  # noqa: E402
    build_footprint_request,
    build_indicator_request,
    build_volume_profile_request,
    read_json_cache,
)
from alfaka.serving.indicators import indicator_specs_from_csv  # noqa: E402


def load_worker_module():
    module_path = ROOT / "systems" / "market-data" / "pods" / "chart-derived-data-worker" / "main.py"
    spec = importlib.util.spec_from_file_location("chart_derived_data_worker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex:
            self.ttls[key] = ex
        return True


class FakeArtifactStore:
    def __init__(self):
        self.rows = []

    def write(self, request, payload):
        self.rows.append((request, payload))


class FakeProvider:
    def __init__(self):
        self.calls = []

    def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None, ma_windows=None):
        self.calls.append({
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "fromTime": from_time,
            "toTime": to_time,
            "maWindows": ma_windows,
        })
        start = datetime(2026, 6, 25, 13, 0, tzinfo=timezone.utc)
        candles = []
        for index in range(limit):
            close = index + 1
            candles.append({
                "timestamp": (start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": close - 0.5,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 100 + index,
            })
        return {"symbol": symbol, "interval": interval, "source": "unit", "feed": "test", "dataStatus": "ready", "candles": candles}

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size):
        self.calls.append({"kind": "volumeProfile", "symbol": symbol, "priceBinSize": price_bin_size})
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "source": "unit",
            "feed": "test",
            "priceBinSize": 0.25,
            "bins": [
                {"priceBin": 100.0, "priceBinSize": 0.25, "volume": 10, "tradeCount": 1, "vwap": 100.1},
                {"priceBin": 100.5, "priceBinSize": 0.25, "volume": 50, "tradeCount": 4, "vwap": 100.6},
            ],
        }

    def footprint_ticks(self, symbol, from_time, to_time, limit=20000):
        self.calls.append({"kind": "footprint", "symbol": symbol, "limit": limit})
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "source": "unit",
            "feed": "test",
            "quotes": [{"timestamp": "2026-06-25T13:30:00.000Z", "bidPrice": 100.0, "askPrice": 100.1}],
            "trades": [
                {"timestamp": "2026-06-25T13:30:01.000Z", "price": 100.1, "size": 10},
                {"timestamp": "2026-06-25T13:30:02.000Z", "price": 100.0, "size": 4},
            ],
        }


class ChartDerivedDataWorkerTest(unittest.TestCase):
    def test_indicator_request_hash_and_worker_result_are_shared(self):
        worker = load_worker_module()
        provider = FakeProvider()
        redis_client = FakeRedis()
        artifact_store = FakeArtifactStore()
        specs = indicator_specs_from_csv("ma5,ema:5,rsi:14")
        request = build_indicator_request(
            symbol="AAPL",
            interval="1m",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T13:39:00.000Z",
            specs=specs,
            limit=30,
        )

        payload = worker.process_request(request, provider=provider, redis_client=redis_client, artifact_store=artifact_store)

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual([item["id"] for item in payload["indicators"]], ["sma:5", "ema:5", "rsi:14"])
        self.assertEqual(provider.calls[0]["limit"], 45)
        self.assertEqual(provider.calls[0]["fromTime"], "2026-06-25T13:00:00.000Z")
        self.assertEqual(read_json_cache(redis_client, request["cacheKey"])["derived"]["state"], "ready")
        self.assertEqual(artifact_store.rows[0][0]["requestHash"], request["requestHash"])

    def test_volume_profile_request_materializes_request_artifact(self):
        worker = load_worker_module()
        request = build_volume_profile_request(
            symbol="AAPL",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T14:00:00.000Z",
            price_bin_size="auto",
            target_bins=4,
            price_min=100,
            price_max=102,
        )

        payload = worker.process_request(request, provider=FakeProvider(), redis_client=FakeRedis(), artifact_store=FakeArtifactStore())

        self.assertEqual(payload["targetBins"], 4)
        self.assertEqual(payload["poc"]["volume"], 50)
        self.assertEqual(payload["derived"]["artifactStored"], True)

    def test_footprint_request_is_1m_estimated(self):
        worker = load_worker_module()
        request = build_footprint_request(
            symbol="AAPL",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T13:31:00.000Z",
            limit=100,
        )

        payload = worker.process_request(request, provider=FakeProvider(), redis_client=FakeRedis(), artifact_store=FakeArtifactStore())

        self.assertEqual(payload["interval"], "footprint")
        self.assertEqual(payload["sourceInterval"], "1m")
        self.assertEqual(payload["sideClassification"], "estimated")
        self.assertEqual(payload["buckets"][0]["delta"], 6)


if __name__ == "__main__":
    unittest.main()
