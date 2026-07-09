import os
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

from alfaka.alpaca.websocket_collector import read_realtime_subscription_symbols_by_channel
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.realtime.subscription_cohorts import ORDER_FLOW_SOURCE, RealtimeSubscriptionCohortService


class OrderFlowSubscriptionTest(unittest.TestCase):
    def test_orderflow_source_reconciles_pinned_symbols_with_trade_quote_layers(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder()
        controller = RealtimeSubscriptionCohortService(redis, keys)

        controller.replace_order_flow_source(["nvda", "aapl"])

        self.assertEqual(redis.smembers(keys.subscription_source_symbols(ORDER_FLOW_SOURCE)), {"NVDA", "AAPL"})
        self.assertEqual(redis.smembers(keys.subscription_symbols()), {"NVDA", "AAPL"})
        record = redis.hgetall(keys.subscription_symbol("NVDA"))
        self.assertEqual(record["sources"], "orderflow")
        self.assertEqual(record["reason"], "orderflow")
        self.assertEqual(set(record["layers"].split(",")), {"trades", "quotes"})

    def test_orderflow_source_survives_realtime_cap_priority(self):
        redis = _MemoryRedis()
        controller = RealtimeSubscriptionCohortService(redis)

        controller.replace_order_flow_source(["NVDA", "AAPL"])
        controller.refresh_active_chart("user-a", "session-1", "MSFT", 90)
        controller.refresh_active_chart("user-a", "session-2", "TSLA", 90)

        with mock.patch.dict(os.environ, {"ALPACA_MAX_TRADE_SYMBOLS": "2"}, clear=False):
            desired = read_realtime_subscription_symbols_by_channel(redis, ["trades", "quotes"])

        self.assertEqual(desired["trades"], {"AAPL", "NVDA"})
        self.assertEqual(desired["quotes"], {"AAPL", "NVDA"})


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.sets = {}
        self.lists = {}
        self.streams = {}
        self.expirations = {}

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def hset(self, key, mapping=None, **kwargs):
        values = dict(mapping or kwargs)
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.sets.pop(key, None)
        self.lists.pop(key, None)
        return 1

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    def exists(self, key):
        return key in self.values or key in self.hashes or key in self.sets or key in self.lists

    def expire(self, key, seconds):
        self.expirations[key] = seconds
        return True

    def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def xadd(self, key, fields):
        self.streams.setdefault(key, []).append(fields)
        return len(self.streams[key])


if __name__ == "__main__":
    unittest.main()
