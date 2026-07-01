from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/api-server/pods/api-server/gops-backend"))
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from app.services import alfaka_market_data  # noqa: E402
from alfaka.common.redis_keys import RedisKeyBuilder  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}

    def delete(self, key: str) -> None:
        self.sets.pop(key, None)
        self.strings.pop(key, None)

    def sadd(self, key: str, *values: str) -> None:
        self.sets.setdefault(key, set()).update(values)

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    def get(self, key: str) -> str | None:
        return self.strings.get(key)


class FakeClickHouseProvider:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[list[str], int]] = []

    def hot_symbols_by_dollar_volume(self, symbols, limit=10):
        self.calls.append((list(symbols), limit))
        return self.rows[:limit]


class FakeCandleProvider:
    def __init__(self, candles_by_interval: dict[str, list[dict[str, object]]]) -> None:
        self.clickhouse_provider = SimpleNamespace(
            candles=lambda symbol, interval, limit: candles_by_interval.get(interval, [])[:limit]
        )


class WatchlistSyncTest(unittest.TestCase):
    def test_replace_watchlist_symbols_clears_existing_redis_set_when_empty(self) -> None:
        redis = FakeRedis()
        key = RedisKeyBuilder().watchlist_symbols()
        redis.sadd(key, "AAPL", "MSFT")
        provider = SimpleNamespace(redis_provider=SimpleNamespace(redis=redis))

        with mock.patch.object(alfaka_market_data, "get_market_data_provider", return_value=provider), \
                mock.patch.object(alfaka_market_data, "symbol_summaries_for", side_effect=lambda symbols: [{"symbol": symbol} for symbol in symbols]):
            payload = alfaka_market_data.replace_watchlist_symbols([])

        self.assertEqual(payload["symbols"], [])
        self.assertEqual(redis.smembers(key), set())

    def test_replace_watchlist_symbols_persists_normalized_unique_symbols(self) -> None:
        redis = FakeRedis()
        key = RedisKeyBuilder().watchlist_symbols()
        provider = SimpleNamespace(redis_provider=SimpleNamespace(redis=redis))

        with mock.patch.object(alfaka_market_data, "get_market_data_provider", return_value=provider), \
                mock.patch.object(alfaka_market_data, "symbol_summaries_for", side_effect=lambda symbols: [{"symbol": symbol} for symbol in symbols]):
            payload = alfaka_market_data.replace_watchlist_symbols(["aapl", "MSFT", "AAPL"])

        self.assertEqual(payload["symbols"], [{"symbol": "AAPL"}, {"symbol": "MSFT"}])
        self.assertEqual(redis.smembers(key), {"AAPL", "MSFT"})

    def test_hot_symbol_summaries_persists_hot_tier_symbols_and_snapshot(self) -> None:
        redis = FakeRedis()
        snapshot = {
            "ranking": {"method": "current_session_dollar_volume", "limit": 3},
            "symbols": [
                {"symbol": "MSFT", "lastPrice": 1, "sessionDollarVolume": 300},
                {"symbol": "AAPL", "lastPrice": 1, "sessionDollarVolume": 200},
                {"symbol": "MSFT", "lastPrice": 1, "sessionDollarVolume": 100},
            ],
        }
        redis_provider = SimpleNamespace(
            redis=redis,
            hot_symbols_snapshot=lambda: snapshot,
            latest_price=lambda symbol: {},
        )
        provider = SimpleNamespace(redis_provider=redis_provider, symbol_detail=lambda symbol: {})

        with mock.patch.object(alfaka_market_data, "get_market_data_provider", return_value=provider):
            payload = alfaka_market_data.hot_symbol_summaries(limit=2)

        keys = RedisKeyBuilder()
        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["MSFT", "AAPL"])
        self.assertEqual(redis.smembers(keys.hot_symbols()), {"MSFT", "AAPL"})
        persisted_snapshot = json.loads(redis.get(keys.hot_symbols_snapshot()) or "{}")
        self.assertEqual([item["symbol"] for item in persisted_snapshot["symbols"]], ["MSFT", "AAPL"])

    def test_hot_symbol_summaries_recomputes_when_snapshot_cannot_fill_requested_limit(self) -> None:
        redis = FakeRedis()
        old_snapshot = {
            "ranking": {
                "method": "current_session_dollar_volume",
                "limit": 3,
                "asOf": "2026-06-30T00:00:00.000Z",
                "sourceUpdatedAt": "2026-06-30T00:00:00.000Z",
                "refreshSeconds": 60,
            },
            "symbols": [
                {"symbol": "JPM", "lastPrice": 1, "sessionDollarVolume": 300},
                {"symbol": "WMT", "lastPrice": 1, "sessionDollarVolume": 200},
                {"symbol": "AAPL", "lastPrice": 1, "sessionDollarVolume": 100},
            ],
        }
        redis.strings[RedisKeyBuilder().hot_symbols_snapshot()] = json.dumps(old_snapshot)
        clickhouse_rows = [
            {"symbol": f"T{i}", "lastPrice": 10 + i, "sessionDollarVolume": 1000 - i, "volume": 100 + i}
            for i in range(10)
        ]
        clickhouse = FakeClickHouseProvider(clickhouse_rows)
        redis_provider = SimpleNamespace(
            redis=redis,
            hot_symbols_snapshot=lambda: json.loads(redis.get(RedisKeyBuilder().hot_symbols_snapshot()) or "{}"),
            latest_price=lambda symbol: {},
        )
        provider = SimpleNamespace(
            redis_provider=redis_provider,
            clickhouse_provider=clickhouse,
            symbol_detail=lambda symbol: {"name": symbol, "market": "US"},
        )

        with mock.patch.object(alfaka_market_data, "get_market_data_provider", return_value=provider), \
                mock.patch.object(alfaka_market_data, "configured_universe_symbols", return_value=[f"T{i}" for i in range(20)]):
            payload = alfaka_market_data.hot_symbol_summaries(limit=10)

        keys = RedisKeyBuilder()
        self.assertEqual(len(payload["symbols"]), 10)
        self.assertEqual([item["symbol"] for item in payload["symbols"]], [f"T{i}" for i in range(10)])
        self.assertEqual(redis.smembers(keys.hot_symbols()), {f"T{i}" for i in range(10)})
        self.assertEqual(len(json.loads(redis.get(keys.hot_symbols_snapshot()) or "{}")["symbols"]), 10)
        self.assertEqual(clickhouse.calls[0][1], 10)

    def test_hot_symbol_summaries_fills_partial_clickhouse_top_ten_from_universe(self) -> None:
        redis = FakeRedis()
        clickhouse_rows = [
            {"symbol": f"T{i}", "lastPrice": 10 + i, "sessionDollarVolume": 1000 - i, "volume": 100 + i}
            for i in range(3)
        ]
        clickhouse = FakeClickHouseProvider(clickhouse_rows)
        redis_provider = SimpleNamespace(
            redis=redis,
            hot_symbols_snapshot=lambda: None,
            latest_price=lambda symbol: {},
        )
        provider = SimpleNamespace(
            redis_provider=redis_provider,
            clickhouse_provider=clickhouse,
            symbol_detail=lambda symbol: {"name": symbol, "market": "US"},
        )

        def fallback_record(_provider, symbol):
            index = int(symbol[1:])
            return {
                "symbol": symbol,
                "lastPrice": 10 + index,
                "sessionDollarVolume": 1000 - index,
                "volume": 100 + index,
                "rankReason": "test_fallback",
            }

        with mock.patch.object(alfaka_market_data, "get_market_data_provider", return_value=provider), \
                mock.patch.object(alfaka_market_data, "configured_universe_symbols", return_value=[f"T{i}" for i in range(20)]), \
                mock.patch.object(alfaka_market_data, "build_hot_symbol_record", side_effect=fallback_record), \
                mock.patch.dict(os.environ, {"HOT_TIER_FALLBACK_SCAN_LIMIT": "20"}, clear=False):
            payload = alfaka_market_data.hot_symbol_summaries(limit=10)

        keys = RedisKeyBuilder()
        self.assertEqual(len(payload["symbols"]), 10)
        self.assertEqual([item["symbol"] for item in payload["symbols"]], [f"T{i}" for i in range(10)])
        self.assertEqual(redis.smembers(keys.hot_symbols()), {f"T{i}" for i in range(10)})
        self.assertEqual(len(json.loads(redis.get(keys.hot_symbols_snapshot()) or "{}")["symbols"]), 10)
        self.assertEqual(clickhouse.calls[0][1], 10)

    def test_hot_symbol_summaries_defaults_to_top_ten(self) -> None:
        redis = FakeRedis()
        clickhouse_rows = [
            {"symbol": f"T{i}", "lastPrice": 10 + i, "sessionDollarVolume": 1000 - i, "volume": 100 + i}
            for i in range(12)
        ]
        clickhouse = FakeClickHouseProvider(clickhouse_rows)
        redis_provider = SimpleNamespace(
            redis=redis,
            hot_symbols_snapshot=lambda: None,
            latest_price=lambda symbol: {},
        )
        provider = SimpleNamespace(
            redis_provider=redis_provider,
            clickhouse_provider=clickhouse,
            symbol_detail=lambda symbol: {"name": symbol, "market": "US"},
        )

        with mock.patch.object(alfaka_market_data, "get_market_data_provider", return_value=provider), \
                mock.patch.object(alfaka_market_data, "configured_universe_symbols", return_value=[f"T{i}" for i in range(20)]), \
                mock.patch.dict(os.environ, {}, clear=True):
            payload = alfaka_market_data.hot_symbol_summaries()

        keys = RedisKeyBuilder()
        self.assertEqual(payload["ranking"]["limit"], 10)
        self.assertEqual(len(payload["symbols"]), 10)
        self.assertEqual(redis.smembers(keys.hot_symbols()), {f"T{i}" for i in range(10)})
        self.assertEqual(len(json.loads(redis.get(keys.hot_symbols_snapshot()) or "{}")["symbols"]), 10)
        self.assertEqual(clickhouse.calls[0][1], 10)

    def test_change_percent_ignores_stale_previous_daily_close(self) -> None:
        provider = FakeCandleProvider({
            "1D": [
                {"timestamp": "2024-01-31T00:00:00.000Z", "close": 167.69},
                {"timestamp": "2026-06-30T00:00:00.000Z", "close": 580.91},
            ],
            "1m": [],
        })

        change = alfaka_market_data._change_percent_from_previous_close(
            provider,
            "JPM",
            577.5,
            candles=None,
            anchor_timestamp="2026-06-30T22:05:00.000Z",
        )

        self.assertIsNone(change)

    def test_change_percent_uses_recent_previous_daily_close(self) -> None:
        provider = FakeCandleProvider({
            "1D": [
                {"timestamp": "2026-06-29T00:00:00.000Z", "close": 100},
                {"timestamp": "2026-06-30T00:00:00.000Z", "close": 110},
            ],
            "1m": [],
        })

        change = alfaka_market_data._change_percent_from_previous_close(
            provider,
            "AAPL",
            112,
            candles=None,
            anchor_timestamp="2026-06-30T22:05:00.000Z",
        )

        self.assertEqual(change, 12.0)


if __name__ == "__main__":
    unittest.main()
