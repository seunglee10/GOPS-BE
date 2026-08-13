import unittest
import json
import types
from unittest import mock

from market_data.common.redis_keys import RedisKeyBuilder
from market_data.orderflow import OrderFlowBinBuilder, PinnedQuoteCache
from market_data.orderflow.redis_model import encode_order_flow_minute_blob, order_flow_minute_blob
from market_data.serving.redis_provider import RedisMarketDataProvider
from market_data.streaming.processor import (
    maybe_publish_order_flow_event,
    process_order_flow_live_path,
    restore_order_flow_live_minutes,
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
        now[0] = 11.0
        refreshed = cache.quote_for("NVDA")

        self.assertEqual(first["bidPrice"], 158.33)
        self.assertEqual(cached["askPrice"], 158.35)
        self.assertEqual(refreshed["bidPrice"], 158.34)

    def test_quote_cache_miss_loads_redis_once_per_symbol_per_second(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        now = [10.0]
        cache = PinnedQuoteCache(redis, keys, refresh_ms=150, clock=lambda: now[0])

        for _index in range(100):
            self.assertIsNone(cache.quote_for("NVDA"))
        self.assertEqual(redis.get_calls, 1)

        now[0] = 11.0
        self.assertIsNone(cache.quote_for("NVDA"))
        self.assertEqual(redis.get_calls, 2)

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

    def test_live_minute_flush_respects_interval_and_keeps_latest_builder_state(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder, order_flow_redis_flush_state={})
        clock = [100.0]

        with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
            first = builder.update(_trade(timestamp="2026-07-09T13:30:10.000Z", price=158.341, size=10), "ask")
            write_order_flow_bin_to_redis(redis, keys, first, state=state)
            clock[0] = 100.10
            second = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.351, size=5), "bid")
            write_order_flow_bin_to_redis(redis, keys, second, state=state)
            clock[0] = 100.26
            third = builder.update(_trade(timestamp="2026-07-09T13:30:30.000Z", price=158.361, size=3), "unknown")
            write_order_flow_bin_to_redis(redis, keys, third, state=state)

        self.assertEqual([call["key"] for call in redis.set_calls], [
            keys.order_flow_live_minute("NVDA"),
            keys.order_flow_live_minute("NVDA"),
        ])
        blob = json.loads(redis.values[keys.order_flow_live_minute("NVDA")])
        self.assertEqual(blob["eventMinute"], "2026-07-09T13:30:00.000Z")
        self.assertEqual(sum(item["askVolume"] for item in blob["bins"]), 10)
        self.assertEqual(sum(item["bidVolume"] for item in blob["bins"]), 5)
        self.assertEqual(sum(item["unknownVolume"] for item in blob["bins"]), 3)
        self.assertEqual(redis.expirations[keys.order_flow_live_minute("NVDA")], 300)

    def test_minute_close_forces_zadd_before_next_live_minute(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder, order_flow_redis_flush_state={})
        clock = [100.0]

        with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
            first = builder.update(_trade(timestamp="2026-07-09T13:30:10.000Z", price=158.341, size=10), "ask")
            write_order_flow_bin_to_redis(redis, keys, first, state=state)
            clock[0] = 100.05
            second = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.351, size=5), "bid")
            write_order_flow_bin_to_redis(redis, keys, second, state=state)
            next_minute = builder.update(_trade(timestamp="2026-07-09T13:31:00.000Z", price=158.361, size=3), "unknown")
            write_order_flow_bin_to_redis(redis, keys, next_minute, state=state)

        closed_values = redis.zsets[keys.order_flow_minutes("NVDA")]
        self.assertEqual(len(closed_values), 1)
        closed_blob = json.loads(next(iter(closed_values)))
        self.assertEqual(closed_blob["eventMinute"], "2026-07-09T13:30:00.000Z")
        self.assertEqual(sum(item["askVolume"] for item in closed_blob["bins"]), 10)
        self.assertEqual(sum(item["bidVolume"] for item in closed_blob["bins"]), 5)
        self.assertEqual(redis.expirations[keys.order_flow_minutes("NVDA")], 86400)
        live_blob = json.loads(redis.values[keys.order_flow_live_minute("NVDA")])
        self.assertEqual(live_blob["eventMinute"], "2026-07-09T13:31:00.000Z")

    def test_session_rollover_deletes_minutes_and_live_minute_keys(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder, order_flow_redis_flush_state={})
        first = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.341, size=10), "ask")
        write_order_flow_bin_to_redis(redis, keys, first, state=state)
        redis.zsets[keys.order_flow_minutes("NVDA")] = {"stale": 1}
        redis.values[keys.order_flow_live_minute("NVDA")] = "{}"
        second = builder.update(_trade(timestamp="2026-07-10T13:30:20.000Z", price=159.101, size=5), "bid")

        write_order_flow_bin_to_redis(redis, keys, second, state=state)

        self.assertEqual(redis.deleted[:2], [
            keys.order_flow_minutes("NVDA"),
            keys.order_flow_live_minute("NVDA"),
        ])
        self.assertNotIn(keys.order_flow_minutes("NVDA"), redis.zsets)
        live_blob = json.loads(redis.values[keys.order_flow_live_minute("NVDA")])
        self.assertEqual(live_blob["sessionDate"], "2026-07-10")

    def test_restart_restore_continues_the_same_live_minute(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        minute = "2026-07-09T13:30:00.000Z"
        live = order_flow_minute_blob("NVDA", minute, [
            _bin(minute, "2026-07-09", 158.34, ask=10),
        ])
        redis.set(keys.order_flow_live_minute("NVDA"), encode_order_flow_minute_blob(live), ex=300)
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder, order_flow_redis_flush_state={})

        self.assertEqual(restore_order_flow_live_minutes(state, redis, keys), 1)
        next_bin = builder.update(_trade(timestamp="2026-07-09T13:30:40.000Z", price=158.34, size=5), "bid")
        write_order_flow_bin_to_redis(redis, keys, next_bin, state=state)

        restored_live = json.loads(redis.values[keys.order_flow_live_minute("NVDA")])
        self.assertEqual(sum(item["askVolume"] for item in restored_live["bins"]), 10)
        self.assertEqual(sum(item["bidVolume"] for item in restored_live["bins"]), 5)

    def test_restart_restore_closes_previous_live_minute_before_next_trade(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        minute = "2026-07-09T13:30:00.000Z"
        live = order_flow_minute_blob("NVDA", minute, [
            _bin(minute, "2026-07-09", 158.34, ask=10),
        ])
        redis.set(keys.order_flow_live_minute("NVDA"), encode_order_flow_minute_blob(live), ex=300)
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(
            order_flow_builder=builder,
            order_flow_quote_cache=None,
            order_flow_quote_max_age_ms=2000,
            order_flow_quote_future_tolerance_ms=250,
            order_flow_publish_state={},
            order_flow_redis_flush_state={},
            order_flow_minutes_ttl_state=set(),
        )

        self.assertEqual(restore_order_flow_live_minutes(state, redis, keys), 1)
        process_order_flow_live_path(
            _trade(timestamp="2026-07-09T13:31:05.000Z", price=158.35, size=3),
            redis,
            keys,
            state,
        )

        closed_values = redis.zsets[keys.order_flow_minutes("NVDA")]
        closed_blob = json.loads(next(iter(closed_values)))
        self.assertEqual(closed_blob["eventMinute"], minute)
        self.assertEqual(sum(item["askVolume"] for item in closed_blob["bins"]), 10)
        current_live = json.loads(redis.values[keys.order_flow_live_minute("NVDA")])
        self.assertEqual(current_live["eventMinute"], "2026-07-09T13:31:00.000Z")

    def test_redis_provider_reads_new_layout_first_and_live_minute_overrides_closed(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        provider = RedisMarketDataProvider.__new__(RedisMarketDataProvider)
        provider.redis = redis
        provider.keys = keys
        closed = order_flow_minute_blob("NVDA", "2026-07-09T13:30:00.000Z", [
            _bin("2026-07-09T13:30:00.000Z", "2026-07-09", 100.0, ask=1),
        ])
        live = order_flow_minute_blob("NVDA", "2026-07-09T13:30:00.000Z", [
            _bin("2026-07-09T13:30:00.000Z", "2026-07-09", 100.0, ask=2, bid=3),
        ])
        redis.zadd(keys.order_flow_minutes("NVDA"), {encode_order_flow_minute_blob(closed): 1780000000})
        redis.set(keys.order_flow_live_minute("NVDA"), encode_order_flow_minute_blob(live), ex=300)
        bins = provider.order_flow_live_bins("NVDA")

        self.assertEqual(len(bins), 1)
        self.assertEqual(bins[0]["eventMinute"], "2026-07-09T13:30:00.000Z")
        self.assertEqual(bins[0]["askVolume"], 2)
        self.assertEqual(bins[0]["bidVolume"], 3)

    def test_redis_provider_does_not_read_retired_hash_when_minute_keys_are_empty(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        provider = RedisMarketDataProvider.__new__(RedisMarketDataProvider)
        provider.redis = redis
        provider.keys = keys

        bins = provider.order_flow_live_bins("NVDA")

        self.assertEqual(bins, [])
        self.assertEqual(redis.hgetall_calls, 0)

    def test_publish_throttle_and_minute_rollover_flush(self):
        redis = _MemoryRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(order_flow_builder=builder, order_flow_publish_state={})
        clock = [100.0]

        first = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", price=158.34, size=10), "ask")
        with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
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


def _bin(minute, session_date, price, *, ask=0, bid=0, unknown=0):
    return {
        "eventType": "ORDER_FLOW_BIN",
        "eventMinute": minute,
        "sessionDate": session_date,
        "symbol": "NVDA",
        "priceBin": price,
        "priceBinSize": 0.01,
        "askVolume": ask,
        "bidVolume": bid,
        "unknownVolume": unknown,
        "askTradeCount": 1 if ask else 0,
        "bidTradeCount": 1 if bid else 0,
        "unknownTradeCount": 1 if unknown else 0,
        "volume": ask + bid + unknown,
        "tradeCount": int(bool(ask)) + int(bool(bid)) + int(bool(unknown)),
        "source": "alpaca",
        "feed": "sip",
        "marketSession": "regular",
    }


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.zsets = {}
        self.expirations = {}
        self.published = []
        self.deleted = []
        self.set_calls = []
        self.get_calls = 0
        self.hgetall_calls = 0

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.set_calls.append({"key": key, "value": value, "ex": ex})
        if ex is not None:
            self.expirations[key] = ex

    def get(self, key):
        self.get_calls += 1
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

    def hgetall(self, key):
        self.hgetall_calls += 1
        return dict(self.hashes.get(key, {}))

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrangebyscore(self, key, min_score, max_score, start=None, num=None):
        def includes(score):
            lower = float("-inf") if min_score == "-inf" else float(min_score)
            upper = float("inf") if max_score == "+inf" else float(max_score)
            return lower <= float(score) <= upper

        rows = [member for member, score in sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1]) if includes(score)]
        if start is not None and num is not None:
            return rows[int(start):int(start) + int(num)]
        return rows

    def delete(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.zsets.pop(key, None)
        return 1

    def expire(self, key, seconds):
        self.expirations[key] = seconds
        return True

    def publish(self, channel, value):
        self.published.append((channel, value))
        return 1


if __name__ == "__main__":
    unittest.main()
