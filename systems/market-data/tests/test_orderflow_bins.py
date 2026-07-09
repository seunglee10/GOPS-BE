import unittest
import json
import types
from unittest import mock

from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.orderflow import OrderFlowBinBuilder, PinnedQuoteCache
from alfaka.streaming.processor import (
    maybe_publish_order_flow_event,
    process_order_flow_live_path,
    write_order_flow_bin_to_redis,
)


class OrderFlowBinBuilderTest(unittest.TestCase):
    def test_accumulates_side_split_volume_and_counts(self):
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))

        first = builder.update(_trade(price=158.341, size=10), "ask")
        second = builder.update(_trade(price=158.339, size=4), "bid")
        third = builder.update(_trade(price=158.341, size=2), "unknown")

        self.assertIsNotNone(first)
        self.assertEqual(second["priceBin"], 158.34)
        self.assertEqual(third["askVolume"], 10)
        self.assertEqual(third["bidVolume"], 4)
        self.assertEqual(third["unknownVolume"], 2)
        self.assertEqual(third["askTradeCount"], 1)
        self.assertEqual(third["bidTradeCount"], 1)
        self.assertEqual(third["unknownTradeCount"], 1)
        self.assertEqual(third["volume"], 16)
        self.assertEqual(third["tradeCount"], 3)
        self.assertEqual(third["classificationVersion"], "orderflow-estimated-v2")
        self.assertEqual(third["sideClassification"], "estimated")

    def test_skips_unpinned_non_regular_and_invalid_trades(self):
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))

        self.assertIsNone(builder.update(_trade(symbol="AAPL"), "ask"))
        self.assertIsNone(builder.update(_trade(marketSession="after"), "ask"))
        self.assertIsNone(builder.update(_trade(size=0), "ask"))
        self.assertIsNone(builder.update({"symbol": "NVDA", "marketSession": "regular", "price": 1}, "ask"))
        self.assertEqual(builder.bins, {})

    def test_session_date_uses_eastern_date_and_after_session_is_skipped(self):
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))

        regular = builder.update(_trade(timestamp="2026-07-09T23:59:00.000Z"), "ask")
        after = builder.update(_trade(timestamp="2026-07-10T00:01:00.000Z", marketSession="after"), "ask")

        self.assertEqual(regular["sessionDate"], "2026-07-09")
        self.assertIsNone(after)

    def test_minute_rollover_and_stale_eviction(self):
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        builder.update(_trade(timestamp="2026-07-09T13:30:10.000Z", price=158.34, size=1), "ask")
        builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.34, size=1), "ask")
        builder.update(_trade(timestamp="2026-07-09T13:31:10.000Z", price=158.35, size=2), "bid")
        latest = builder.update(_trade(timestamp="2026-07-09T13:34:00.000Z", price=158.36, size=3), "ask")

        self.assertEqual(latest["eventMinute"], "2026-07-09T13:34:00.000Z")
        self.assertEqual(builder.bins_for_minute("NVDA", "2026-07-09T13:30:00.000Z"), [])
        self.assertEqual(len(builder.bins_for_minute("NVDA", "2026-07-09T13:31:00.000Z")), 1)
        self.assertEqual(builder.sweep_count, 3)

    def test_pinned_quote_cache_refreshes_from_live_quote_key(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        redis.set(keys.live_quote("NVDA"), json.dumps({"bid_price": "158.33", "ask_price": "158.35"}))
        now = [10.0]
        cache = PinnedQuoteCache(redis, keys, refresh_ms=150, clock=lambda: now[0])

        first = cache.quote_for("nvda")
        redis.set(keys.live_quote("NVDA"), json.dumps({"bidPrice": 158.34, "askPrice": 158.36}))
        cached = cache.quote_for("NVDA")
        now[0] = 10.2
        refreshed = cache.quote_for("NVDA")

        self.assertEqual(first["bidPrice"], 158.33)
        self.assertEqual(cached["askPrice"], 158.35)
        self.assertEqual(refreshed["bidPrice"], 158.34)

    def test_live_path_marks_stale_quote_unknown_for_any_pinned_symbol(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        redis.set(keys.live_quote("NVDA"), json.dumps({
            "timestamp": "2026-07-09T13:30:00.000Z",
            "bidPrice": 158.33,
            "askPrice": 158.35,
        }))
        state = types.SimpleNamespace(
            order_flow_builder=OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA", "AAPL"})),
            order_flow_quote_cache=PinnedQuoteCache(redis, keys, refresh_ms=150),
            order_flow_quote_max_age_ms=2000,
            order_flow_quote_future_tolerance_ms=250,
            order_flow_publish_state={},
        )

        of_bin = process_order_flow_live_path(
            _trade(timestamp="2026-07-09T13:30:10.000Z", price=159.0, size=7),
            redis,
            keys,
            state,
        )

        self.assertEqual(of_bin["askVolume"], 0)
        self.assertEqual(of_bin["bidVolume"], 0)
        self.assertEqual(of_bin["unknownVolume"], 7)
        self.assertEqual(of_bin["unknownTradeCount"], 1)

    def test_redis_hash_write_field_format_ttl_and_session_rollover_delete(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder)
        first = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.341, size=10), "ask")
        write_order_flow_bin_to_redis(redis, keys, first, state=state)
        redis.hashes[keys.order_flow_live("NVDA")]["stale"] = "{}"
        second = {
            **builder.update(_trade(timestamp="2026-07-10T13:30:20.000Z", price=159.101, size=5), "bid"),
            "sessionDate": "2026-07-10",
        }

        write_order_flow_bin_to_redis(redis, keys, second, state=state)

        live_hash = redis.hashes[keys.order_flow_live("NVDA")]
        self.assertEqual(set(live_hash), {"2026-07-10T13:30:00.000Z|159.10"})
        self.assertEqual(json.loads(next(iter(live_hash.values())))["bidVolume"], 5)
        self.assertEqual(redis.expirations[keys.order_flow_live("NVDA")], 86400)
        self.assertEqual(redis.deleted, [keys.order_flow_live("NVDA")])

    def test_publish_throttle_and_minute_rollover_flush(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder, order_flow_publish_state={})
        clock = [100.0]

        first = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.34, size=10), "ask")
        with mock.patch("alfaka.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
            maybe_publish_order_flow_event(redis, keys, state, first)
            clock[0] = 100.1
            same_minute = builder.update(_trade(timestamp="2026-07-09T13:30:30.000Z", price=158.35, size=5), "bid")
            maybe_publish_order_flow_event(redis, keys, state, same_minute)
            clock[0] = 100.15
            next_minute = builder.update(_trade(timestamp="2026-07-09T13:31:00.000Z", price=158.36, size=2), "ask")
            maybe_publish_order_flow_event(redis, keys, state, next_minute)

        market_events = [json.loads(value) for channel, value in redis.published if channel == keys.market_events()]
        self.assertEqual([event["data"]["eventMinute"] for event in market_events], [
            "2026-07-09T13:30:00.000Z",
            "2026-07-09T13:30:00.000Z",
            "2026-07-09T13:31:00.000Z",
        ])
        self.assertEqual(market_events[1]["data"]["bins"][1]["bidVolume"], 5)
        self.assertNotEqual(market_events[0]["eventId"], market_events[1]["eventId"])


def _trade(
    *,
    symbol="NVDA",
    timestamp="2026-07-09T13:30:20.100Z",
    price=158.34,
    size=1,
    marketSession="regular",
):
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "price": price,
        "size": size,
        "feed": "sip",
        "marketSession": marketSession,
    }


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.expirations = {}
        self.published = []
        self.deleted = []

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def hset(self, key, *args, mapping=None, **kwargs):
        if mapping is not None:
            values = mapping
        elif len(args) >= 2:
            values = {args[0]: args[1]}
        else:
            values = kwargs
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def delete(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        return 1

    def expire(self, key, seconds):
        self.expirations[key] = seconds
        return True

    def publish(self, channel, value):
        self.published.append((channel, value))
        return 1


if __name__ == "__main__":
    unittest.main()
