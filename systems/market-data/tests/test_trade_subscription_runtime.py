import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "market-data" / "shared"))

from alfaka.alpaca.websocket_collector import read_hot_symbols, read_trade_subscription_symbols  # noqa: E402
from alfaka.common.redis_keys import RedisKeyBuilder  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}
        self.existing: set[str] = set()

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def get(self, key):
        return self.strings.get(key)

    def exists(self, key):
        return key in self.existing


class TradeSubscriptionRuntimeTest(unittest.TestCase):
    def test_hot_symbols_preserve_snapshot_ranking_before_redis_set_fallback(self):
        keys = RedisKeyBuilder()
        redis = FakeRedis()
        redis.sets[keys.hot_symbols()] = {"STALE1", "STALE2"}
        redis.strings[keys.hot_symbols_snapshot()] = json.dumps({
            "ranking": {"method": "current_session_dollar_volume", "limit": 10},
            "symbols": [{"symbol": f"T{i}"} for i in range(10)],
        })

        self.assertEqual(read_hot_symbols(redis)[:10], [f"T{i}" for i in range(10)])
        self.assertEqual(read_hot_symbols(redis)[10:], ["STALE1", "STALE2"])

    def test_trade_subscription_uses_current_hot_top_ten_before_stale_hot_set(self):
        keys = RedisKeyBuilder()
        redis = FakeRedis()
        redis.sets[keys.active_symbols()] = {"AAPL"}
        redis.existing.add(keys.active_symbol("AAPL"))
        redis.sets[keys.watchlist_symbols()] = {"MSFT", "JPM"}
        redis.sets[keys.hot_symbols()] = {"STALE1", "STALE2"}
        redis.strings[keys.hot_symbols_snapshot()] = json.dumps({
            "ranking": {"method": "current_session_dollar_volume", "limit": 10},
            "symbols": [{"symbol": f"T{i}"} for i in range(10)],
        })

        with mock.patch.dict(os.environ, {
            "ALPACA_MAX_WATCHLIST_TRADE_SYMBOLS": "40",
            "ALPACA_MAX_HOT_TRADE_SYMBOLS": "10",
        }, clear=False):
            symbols = read_trade_subscription_symbols(redis)

        self.assertTrue({"AAPL", "MSFT", "JPM"}.issubset(symbols))
        self.assertTrue({f"T{i}" for i in range(10)}.issubset(symbols))
        self.assertNotIn("STALE1", symbols)
        self.assertNotIn("STALE2", symbols)


if __name__ == "__main__":
    unittest.main()
