import json
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app.market_data.compare.service import ChartCompareService
from app.market_data.fill.service import DistributedFillSingleflight
from app.market_data.query.canonical import CanonicalCandleQuery


class CanonicalChartCompareTest(unittest.TestCase):
    def test_storage_hit_serves_compare_without_alpaca(self):
        provider = _Provider({"NVDA": _candles()})
        fill = _Fill(provider)
        query = CanonicalCandleQuery(provider, fill)
        service = ChartCompareService(
            provider=provider,
            candle_query=query,
            fetcher=lambda *_args: self.fail("compare must not call Alpaca directly"),
            now=lambda: datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc),
        )

        with mock.patch.dict(os.environ, {"CHART_COMPARE_CACHE_ENABLED": "false"}):
            payload = service.snapshot(["NVDA"], "1D")

        self.assertEqual(payload["items"][0]["basePrice"], 100.0)
        self.assertEqual(fill.alpaca_calls, 0)
        self.assertEqual(query.metrics.snapshot()["outcomes"]["redis"], 1)

    def test_compare_miss_fills_and_persists_once_before_reuse(self):
        provider = _Provider()
        fill = _Fill(provider)
        query = CanonicalCandleQuery(provider, fill)
        service = ChartCompareService(
            provider=provider,
            candle_query=query,
            now=lambda: datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc),
        )

        with mock.patch.dict(os.environ, {"CHART_COMPARE_CACHE_ENABLED": "false"}):
            first = service.snapshot(["NVDA"], "1D")
            second = service.snapshot(["NVDA"], "1D")

        self.assertEqual(first["items"], second["items"])
        self.assertEqual(fill.alpaca_calls, 1)
        self.assertEqual(provider.snapshot_calls, 2)

    def test_compare_queries_at_most_three_symbols_concurrently(self):
        query = _ConcurrencyQuery()
        provider = _Provider()
        service = ChartCompareService(provider=provider, candle_query=query)

        with mock.patch.dict(os.environ, {"CHART_COMPARE_CACHE_ENABLED": "false"}):
            service.snapshot(["AAPL", "MSFT", "NVDA", "AMD"], "1D")

        self.assertEqual(query.calls, 4)
        self.assertEqual(query.max_active, 3)

    def test_current_candle_callers_share_the_canonical_query_boundary(self):
        root = Path(__file__).resolve().parents[3]
        backend = root / "systems/api-server/pods/api-server/gops-backend/app"
        query_source = (backend / "market_data/query/service.py").read_text()
        compare_source = (backend / "market_data/compare/service.py").read_text()

        self.assertIn("self.canonical_query.query", query_source)
        self.assertIn("self.candle_query.query", compare_source)
        self.assertNotIn("fetch_alpaca_bars", compare_source)
        self.assertNotIn("self.provider.agent_chart_context", query_source)


class DistributedFillSingleflightTest(unittest.TestCase):
    def test_two_replicas_acquire_one_lock_and_terminal_state_suppresses_requeue(self):
        redis = _ExpiringRedis()
        first = DistributedFillSingleflight(redis, lock_ttl_seconds=2, terminal_ttl_seconds=5)
        second = DistributedFillSingleflight(redis, lock_ttl_seconds=2, terminal_ttl_seconds=5)

        acquired, owner, state = first.acquire("same-range")
        duplicate, _owner, duplicate_state = second.acquire("same-range")
        self.assertTrue(acquired)
        self.assertEqual(state, "queued")
        self.assertFalse(duplicate)
        self.assertEqual(duplicate_state, "already_queued")

        self.assertTrue(first.complete("same-range", owner, "filled"))
        terminal, _owner, terminal_state = second.acquire("same-range")
        self.assertFalse(terminal)
        self.assertEqual(terminal_state, "terminal")

    def test_failed_owner_becomes_retryable_after_lock_ttl(self):
        redis = _ExpiringRedis()
        first = DistributedFillSingleflight(redis, lock_ttl_seconds=2, terminal_ttl_seconds=5)
        second = DistributedFillSingleflight(redis, lock_ttl_seconds=2, terminal_ttl_seconds=5)

        acquired, _owner, _state = first.acquire("crashed-owner")
        self.assertTrue(acquired)
        redis.advance(2.1)
        retried, retry_owner, retry_state = second.acquire("crashed-owner")

        self.assertTrue(retried)
        self.assertIsNotNone(retry_owner)
        self.assertEqual(retry_state, "queued")


class _Provider:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.snapshot_calls = 0

    def candle_snapshot(self, symbol, interval, limit, **_kwargs):
        self.snapshot_calls += 1
        candles = list(self.rows.get(symbol, []))[-limit:]
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
            "_sourceTrace": {
                "redis": {"checked": True, "hit": bool(candles), "rowCount": len(candles)},
                "clickhouse": {"checked": not candles, "hit": False, "rowCount": 0},
            },
        }

    @staticmethod
    def symbol_detail(symbol):
        return {"symbol": symbol, "name": f"{symbol} Corp", "exchange": "NASDAQ"}


class _Fill:
    def __init__(self, provider):
        self.provider = provider
        self.alpaca_calls = 0

    def fill_if_needed(self, *, symbol, payload, **_kwargs):
        source_trace = payload.pop("_sourceTrace", {})
        if not payload.get("candles"):
            self.alpaca_calls += 1
            payload["candles"] = _candles()
            self.provider.rows[symbol] = list(payload["candles"])
            alpaca = {"checked": True, "hit": True, "rowCount": len(payload["candles"])}
        else:
            alpaca = {"checked": False, "hit": False, "rowCount": 0}
        payload["fill"] = {
            "sources": {
                "redis": source_trace.get("redis", {}),
                "clickhouse": source_trace.get("clickhouse", {}),
                "s3": {"checked": False, "hit": False, "rowCount": 0},
                "alpaca": alpaca,
            }
        }
        return payload


class _ConcurrencyQuery:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def query(self, symbol, interval, limit, **_kwargs):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return {"symbol": symbol, "interval": interval, "candles": _candles()[-limit:]}


class _ExpiringRedis:
    def __init__(self):
        self.now = 0.0
        self.values = {}
        self.expires = {}

    def set(self, key, value, nx=False, ex=None):
        self._sweep(key)
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expires[key] = self.now + ex
        return True

    def get(self, key):
        self._sweep(key)
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        self.expires.pop(key, None)

    def expire(self, key, ttl):
        self.expires[key] = self.now + ttl

    def advance(self, seconds):
        self.now += seconds

    def _sweep(self, key):
        if self.expires.get(key, float("inf")) <= self.now:
            self.delete(key)


def _candles():
    return [
        {"timestamp": "2026-07-06T13:30:00.000Z", "close": 100.0},
        {"timestamp": "2026-07-06T14:30:00.000Z", "close": 110.0},
    ]


if __name__ == "__main__":
    unittest.main()
