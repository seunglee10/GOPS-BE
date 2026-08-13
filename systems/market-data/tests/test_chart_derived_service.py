import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "systems" / "api-server" / "pods" / "api-server"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.market_data.derived.service import DerivedCalculationService
from market_data.serving.chart_derived_data import build_indicator_request, build_volume_profile_request
from market_data.serving.indicators import compute_indicator_payload, indicator_specs_from_csv
from market_data.serving.volume_profile import compute_volume_profile_payload


class DerivedCalculationServiceTest(unittest.TestCase):
    def test_ten_concurrent_identical_requests_calculate_once(self):
        redis = _Redis()
        canonical = _CanonicalQuery(_candles())
        service = DerivedCalculationService(canonical_query=canonical, redis_client=redis)
        request = _volume_request()
        calculate_count = 0
        lock = threading.Lock()

        def calculate():
            nonlocal calculate_count
            with lock:
                calculate_count += 1
            payload = service.query_candles("AAPL", "1m", 30, from_time=request["from"], to_time=request["to"], ma_windows=())
            time.sleep(0.02)
            return compute_volume_profile_payload(
                payload,
                symbol="AAPL",
                interval="1m",
                from_time=request["from"],
                to_time=request["to"],
                target_bins=10,
                binning_mode="exact",
            )

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _index: service.resolve(request, calculate), range(10)))

        self.assertEqual(calculate_count, 1)
        self.assertEqual(canonical.reads, 1)
        self.assertTrue(all(result["bins"] == results[0]["bins"] for result in results))
        self.assertTrue(all(result["derived"]["requestHash"] == results[0]["derived"]["requestHash"] for result in results))
        self.assertEqual(service.metrics()["calculate"], 1)
        self.assertEqual(service.metrics()["singleflight_wait"], 9)

    def test_warm_request_reads_redis_without_provider_or_recalculation(self):
        redis = _Redis()
        canonical = _CanonicalQuery(_candles())
        service = DerivedCalculationService(canonical_query=canonical, redis_client=redis)
        request = _indicator_request()

        def calculate():
            payload = service.query_candles("AAPL", "1m", 30, ma_windows=())
            return _indicator_payload(request, payload)

        cold = service.resolve(request, calculate)
        reads_after_cold = canonical.reads
        warm = service.resolve(request, lambda: self.fail("warm request must not calculate"))

        self.assertEqual(reads_after_cold, 1)
        self.assertEqual(canonical.reads, reads_after_cold)
        self.assertEqual(cold["series"], warm["series"])
        self.assertEqual(cold["derived"]["source"], "api-compute")
        self.assertEqual(warm["derived"]["source"], "redis")
        self.assertEqual(service.metrics()["cache_hit"], 1)

    def test_indicator_and_volume_profile_algorithms_match_numeric_golden_fixture(self):
        candles = _candles()
        specs = indicator_specs_from_csv("sma:5,ema:5,rsi:14")
        indicator = compute_indicator_payload(candles, specs)
        timestamps = [f"2026-07-08T13:{30 + index:02d}:00.000Z" for index in range(20)]

        self.assertEqual([item["id"] for item in indicator["indicators"]], ["sma:5", "ema:5", "rsi:14"])
        self.assertEqual([point["timestamp"] for point in indicator["series"]["sma:5"]], timestamps)
        self.assertEqual(
            [point["value"] for point in indicator["series"]["sma:5"]],
            [None, None, None, None, 100.7, 100.8, 100.9, 101.0, 101.1, 101.2, 101.3, 101.4, 101.5, 101.6, 101.7, 101.8, 101.9, 102.0, 102.1, 102.2],
        )
        self.assertEqual(
            [None if point["value"] is None else round(point["value"], 6) for point in indicator["series"]["ema:5"]],
            [None, None, None, None, 100.7, 100.8, 100.9, 101.0, 101.1, 101.2, 101.3, 101.4, 101.5, 101.6, 101.7, 101.8, 101.9, 102.0, 102.1, 102.2],
        )
        self.assertEqual(
            [point["value"] for point in indicator["series"]["rsi:14"]],
            [None] * 14 + [100.0] * 6,
        )

        volume_request = _volume_request()
        kwargs = {
            "symbol": "AAPL",
            "interval": "1m",
            "from_time": volume_request["from"],
            "to_time": volume_request["to"],
            "target_bins": 10,
            "price_min": 99.5,
            "price_max": 103.0,
        }
        volume = compute_volume_profile_payload({"candles": candles, "source": "fixture", "feed": "sip"}, **kwargs)
        self.assertEqual(volume_request["calculationVersion"], "volume-profile-exact-v2")
        self.assertIn("volume-profile-exact-v2", volume_request["cacheKey"])
        self.assertEqual(volume["calculationVersion"], "volume-profile-v1")
        self.assertEqual(volume["bucketCount"], 7)
        self.assertEqual(volume["totalVolume"], 2190.0)
        self.assertEqual(volume["poc"], {
            "index": 3,
            "priceMin": 101.0,
            "priceMax": 101.5,
            "priceMid": 101.25,
            "volume": 550.0,
            "tradeCount": 0,
        })
        self.assertEqual(volume["valueArea"]["bucketIndexes"], [1, 2, 3, 4])
        self.assertEqual(
            [(row["index"], row["priceMin"], row["volume"], row["volumePercent"], row["isPoc"], row["inValueArea"]) for row in volume["bins"]],
            [
                (0, 99.5, 101.33333333, 0.04627093, False, False),
                (1, 100.0, 276.33333333, 0.1261796, False, True),
                (2, 100.5, 459.66666667, 0.20989346, False, True),
                (3, 101.0, 550.0, 0.25114155, True, True),
                (4, 101.5, 453.66666667, 0.20715373, False, True),
                (5, 102.0, 270.33333333, 0.12343988, False, False),
                (6, 102.5, 78.66666667, 0.03592085, False, False),
            ],
        )

    def test_request_time_service_has_no_queue_or_artifact_dependencies(self):
        service = DerivedCalculationService(
            canonical_query=_CanonicalQuery(_candles()),
            redis_client=_Redis(),
        )

        result = service.resolve(_volume_request(), lambda: {"dataStatus": "ready", "bins": []})

        self.assertEqual(result["derived"]["source"], "api-compute")
        self.assertEqual(set(result["derived"]), {"state", "source", "requestHash", "generatedAt"})
        self.assertFalse(hasattr(service, "worker_client"))

    def test_partial_volume_profile_is_not_cached(self):
        redis = _Redis()
        service = DerivedCalculationService(canonical_query=_CanonicalQuery(_candles()), redis_client=redis)
        request = _volume_request(candle_count=21)

        first = service.resolve(request, lambda: {"dataStatus": "partial", "bins": []})
        second = service.resolve(request, lambda: {"dataStatus": "partial", "bins": [{"index": 1}]})

        self.assertEqual(first["dataStatus"], "partial")
        self.assertEqual(second["bins"], [{"index": 1}])
        self.assertNotIn(request["cacheKey"], redis.values)
        self.assertEqual(service.metrics()["calculate"], 2)

    def test_volume_profile_candle_count_changes_request_and_cache_identity(self):
        request_120 = _volume_request(candle_count=120)
        request_200 = _volume_request(candle_count=200)

        self.assertNotEqual(request_120["requestHash"], request_200["requestHash"])
        self.assertNotEqual(request_120["cacheKey"], request_200["cacheKey"])
        self.assertEqual(request_200["limit"], 200)
        self.assertEqual(request_200["parameters"]["candleCount"], 200)


class _Redis:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        del ex
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def setex(self, key, ttl, value):
        del ttl
        with self.lock:
            self.values[key] = value

    def delete(self, key):
        with self.lock:
            self.values.pop(key, None)

    def eval(self, _script, numkeys, key, *args):
        if numkeys != 1:
            raise AssertionError("fixture supports one key")
        with self.lock:
            if self.values.get(key) != args[0]:
                return 0
            if len(args) == 1:
                self.values.pop(key, None)
                return 1
            self.values[key] = args[1]
            return 1


class _CanonicalQuery:
    def __init__(self, candles):
        self.candles = candles
        self.reads = 0
        self.lock = threading.Lock()

    def query(self, symbol, interval, limit, **_kwargs):
        with self.lock:
            self.reads += 1
        return {
            "symbol": symbol,
            "interval": interval,
            "source": "fixture",
            "feed": "sip",
            "dataStatus": "ready",
            "candles": list(self.candles)[-limit:],
        }


def _indicator_request():
    return build_indicator_request(
        symbol="AAPL",
        interval="1m",
        from_time=None,
        to_time=None,
        specs=indicator_specs_from_csv("sma:5,ema:5,rsi:14"),
        limit=30,
    )


def _volume_request(candle_count=None):
    return build_volume_profile_request(
        symbol="AAPL",
        interval="1m",
        from_time="2026-07-08T13:30:00.000Z",
        to_time="2026-07-08T14:00:00.000Z",
        price_bin_size="auto",
        target_bins=10,
        price_min=None,
        price_max=None,
        candle_count=candle_count,
    )


def _indicator_payload(request, candle_payload):
    candles = candle_payload["candles"]
    computed = compute_indicator_payload(candles, indicator_specs_from_csv("sma:5,ema:5,rsi:14"))
    return {
        "symbol": request["symbol"],
        "interval": request["interval"],
        "requestedLimit": 30,
        "lookbackBars": 14,
        "returnedCandleCount": len(candles),
        "source": candle_payload.get("source", "fixture"),
        "feed": candle_payload.get("feed", "sip"),
        "dataStatus": "ready",
        **computed,
    }


def _candles():
    return [
        {
            "timestamp": f"2026-07-08T13:{30 + index:02d}:00.000Z",
            "open": 100 + index * 0.1,
            "high": 101 + index * 0.1,
            "low": 99.5 + index * 0.1,
            "close": 100.5 + index * 0.1,
            "volume": 100 + index,
        }
        for index in range(20)
    ]


if __name__ == "__main__":
    unittest.main()
