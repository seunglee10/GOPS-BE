import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.market_data.derived.service import DerivedCalculationService, normalized_payload
from alfaka.serving.chart_derived_data import build_indicator_request, build_volume_profile_request
from alfaka.serving.indicators import compute_indicator_payload, indicator_specs_from_csv
from alfaka.serving.volume_profile import compute_volume_profile_payload


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
            )

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _index: service.resolve(request, calculate), range(10)))

        self.assertEqual(calculate_count, 1)
        self.assertEqual(canonical.reads, 1)
        self.assertTrue(all(normalized_payload(result) == normalized_payload(results[0]) for result in results))
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

    def test_inline_algorithms_match_worker_fixture_payloads_after_metadata_normalization(self):
        candles = _candles()
        indicator_request = _indicator_request()
        specs = indicator_specs_from_csv("sma:5,ema:5,rsi:14")
        inline_indicator = _indicator_payload(indicator_request, {"candles": candles, "source": "fixture", "feed": "sip"})
        worker_indicator = _indicator_payload(indicator_request, {"candles": candles, "source": "fixture", "feed": "sip"})
        self.assertEqual(normalized_payload(inline_indicator), normalized_payload(worker_indicator))

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
        inline_volume = compute_volume_profile_payload({"candles": candles, "source": "fixture", "feed": "sip"}, **kwargs)
        worker_volume = compute_volume_profile_payload({"candles": candles, "source": "fixture", "feed": "sip"}, **kwargs)
        self.assertEqual(normalized_payload(inline_volume), normalized_payload(worker_volume))
        self.assertEqual([item["id"] for item in compute_indicator_payload(candles, specs)["indicators"]], ["sma:5", "ema:5", "rsi:14"])

    def test_request_time_service_has_no_queue_or_artifact_dependencies(self):
        service = DerivedCalculationService(
            canonical_query=_CanonicalQuery(_candles()),
            redis_client=_Redis(),
        )

        result = service.resolve(_volume_request(), lambda: {"dataStatus": "ready", "bins": []})

        self.assertEqual(result["derived"]["source"], "api-compute")
        self.assertFalse(hasattr(service, "worker_client"))


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


def _volume_request():
    return build_volume_profile_request(
        symbol="AAPL",
        interval="1m",
        from_time="2026-07-08T13:30:00.000Z",
        to_time="2026-07-08T14:00:00.000Z",
        price_bin_size="auto",
        target_bins=10,
        price_min=None,
        price_max=None,
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
