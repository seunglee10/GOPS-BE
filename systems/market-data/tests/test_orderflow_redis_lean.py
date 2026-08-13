import json
import os
import types
import unittest
from collections import Counter
from unittest import mock

from market_data.common.market_messages import build_raw_envelope
from market_data.common.redis_keys import RedisKeyBuilder
from market_data.orderflow import OrderFlowBinBuilder
from market_data.streaming.processor import (
    ProcessorState,
    configure_order_flow_state,
    process_raw_envelope,
    write_order_flow_bin_to_redis,
    write_processor_health,
    write_trade_to_redis,
)
from market_data.streaming.transforms import normalize_quote


class RedisLeanHotPathTest(unittest.TestCase):
    def test_quote_path_redis_commands_are_constant_inside_throttle_window(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")
        state = ProcessorState()
        state.active_feed_cache = _accepted_feed_cache()
        producer = _Producer()
        clock = [100.0]

        with mock.patch.dict(os.environ, {
            "QUOTE_REDIS_WRITE_MIN_INTERVAL_MS": "100",
            "QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS": "250",
            "HEALTH_WRITE_MIN_INTERVAL_MS": "1000",
        }):
            with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
                for index in range(5):
                    result = process_raw_envelope(
                        _quote_envelope(index, symbol="AAPL"),
                        producer,
                        redis,
                        keys,
                        state,
                        _topics(),
                    )
                    self.assertEqual(result, "quotes")

                self.assertEqual(redis.command_count("set", keys.live_quote("AAPL")), 1)
                self.assertEqual(redis.command_count("publish", keys.market_events()), 1)
                self.assertEqual(redis.command_count("set", keys.component_health("market-processor")), 1)
                self.assertEqual(redis.command_count("set", keys.component_health("market-processor:symbol:AAPL")), 1)
                self.assertEqual(redis.command_count("set", keys.component_health("market-processor:feed:sip")), 1)
                self.assertEqual(redis.total_commands(), 5)

                clock[0] = 100.251
                process_raw_envelope(_quote_envelope(6, symbol="AAPL"), producer, redis, keys, state, _topics())

        self.assertEqual(redis.command_count("set", keys.live_quote("AAPL")), 2)
        self.assertEqual(redis.command_count("publish", keys.market_events()), 2)
        self.assertEqual(redis.command_count("set", keys.component_health("market-processor")), 1)

    def test_trade_live_state_write_is_symbol_throttled(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")
        state = ProcessorState()
        clock = [200.0]

        with mock.patch.dict(os.environ, {"TRADE_REDIS_WRITE_MIN_INTERVAL_MS": "250"}):
            with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
                for index in range(5):
                    write_trade_to_redis(redis, keys, _trade(trade_id=index), state=state)

                self.assertEqual(redis.command_count("hset", keys.live_trade("NVDA")), 1)
                self.assertEqual(redis.command_count("expire", keys.live_trade("NVDA")), 1)

                clock[0] = 200.251
                write_trade_to_redis(redis, keys, _trade(trade_id=6), state=state)

        self.assertEqual(redis.command_count("hset", keys.live_trade("NVDA")), 2)
        self.assertEqual(redis.command_count("expire", keys.live_trade("NVDA")), 2)

    def test_order_flow_flush_commands_do_not_scale_with_trade_count(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")
        builder = OrderFlowBinBuilder(price_bin_size=0.01, pinned_symbols=frozenset({"NVDA"}))
        state = types.SimpleNamespace(
            order_flow_builder=builder,
            order_flow_redis_flush_state={},
            order_flow_minutes_ttl_state=set(),
        )
        clock = [300.0]

        with mock.patch.dict(os.environ, {
            "ORDER_FLOW_REDIS_FLUSH_MS": "250",
            "ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS": "300",
            "ORDER_FLOW_LIVE_TTL_SECONDS": "86400",
        }):
            with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
                first = builder.update(_trade(timestamp="2026-07-09T13:30:05.000Z", trade_id=1), "ask")
                write_order_flow_bin_to_redis(redis, keys, first, state=state)
                for index in range(2, 6):
                    clock[0] = 300.10
                    same_window = builder.update(
                        _trade(timestamp=f"2026-07-09T13:30:0{index}.100Z", trade_id=index),
                        "bid",
                    )
                    write_order_flow_bin_to_redis(redis, keys, same_window, state=state)

                self.assertEqual(redis.command_count("set", keys.order_flow_live_minute("NVDA")), 1)

                clock[0] = 300.251
                next_flush = builder.update(_trade(timestamp="2026-07-09T13:30:20.000Z", trade_id=6), "unknown")
                write_order_flow_bin_to_redis(redis, keys, next_flush, state=state)

                next_minute = builder.update(_trade(timestamp="2026-07-09T13:31:00.000Z", trade_id=7), "ask")
                write_order_flow_bin_to_redis(redis, keys, next_minute, state=state)

                another_minute = builder.update(_trade(timestamp="2026-07-09T13:32:00.000Z", trade_id=8), "ask")
                write_order_flow_bin_to_redis(redis, keys, another_minute, state=state)

        self.assertEqual(redis.command_count("set", keys.order_flow_live_minute("NVDA")), 4)
        self.assertEqual(redis.command_count("zadd", keys.order_flow_minutes("NVDA")), 2)
        self.assertEqual(redis.command_count("expire", keys.order_flow_minutes("NVDA")), 1)

    def test_health_write_is_interval_throttled(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")
        state = ProcessorState()
        envelope = _quote_envelope(1, symbol="AAPL")
        clock = [400.0]

        with mock.patch.dict(os.environ, {"HEALTH_WRITE_MIN_INTERVAL_MS": "1000"}):
            with mock.patch("market_data.streaming.processor.time.monotonic", side_effect=lambda: clock[0]):
                for _index in range(5):
                    write_processor_health(redis, keys, envelope, result="quotes", state=state)

                self.assertEqual(redis.command_count("set", keys.component_health("market-processor")), 1)
                self.assertEqual(redis.command_count("set", keys.component_health("market-processor:symbol:AAPL")), 1)
                self.assertEqual(redis.command_count("set", keys.component_health("market-processor:feed:sip")), 1)

                clock[0] = 401.0
                write_processor_health(redis, keys, envelope, result="quotes", state=state)

        self.assertEqual(redis.command_count("set", keys.component_health("market-processor")), 2)
        self.assertEqual(redis.command_count("set", keys.component_health("market-processor:symbol:AAPL")), 2)
        self.assertEqual(redis.command_count("set", keys.component_health("market-processor:feed:sip")), 2)

    def test_order_flow_quote_cache_only_updates_memory_without_redis_get_or_publish(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")
        state = ProcessorState(order_flow_quote_cache_only=True)
        state.active_feed_cache = _accepted_feed_cache()
        producer = _Producer()

        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            configure_order_flow_state(state, redis, keys)
            startup_get_count = redis.counts["get"]
            result = process_raw_envelope(_quote_envelope(1, symbol="NVDA"), producer, redis, keys, state, _topics())
            process_raw_envelope(_quote_envelope(2, symbol="AAPL"), producer, redis, keys, state, _topics())

        quote = state.order_flow_quote_cache.quote_for("NVDA")
        self.assertEqual(result, "quotes_cached")
        self.assertEqual(quote["bidPrice"], 100.01)
        self.assertEqual(quote["askPrice"], 100.11)
        self.assertIsNone(state.order_flow_quote_cache.quote_for("AAPL"))
        self.assertEqual(startup_get_count, 1)
        self.assertEqual(redis.counts["get"], startup_get_count)
        self.assertEqual(redis.counts["publish"], 0)
        self.assertEqual(redis.command_count("set", keys.live_quote("NVDA")), 0)
        self.assertEqual(producer.sent, [])

    def test_reproduced_legacy_quote_baseline_counts_match_documented_table(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")

        for index in range(5):
            quote = normalize_quote(_quote_envelope(index, symbol="AAPL"))
            _legacy_write_quote(redis, keys, quote)
            _legacy_publish_quote(redis, keys, quote)
            _legacy_write_health(redis, keys, _quote_envelope(index, symbol="AAPL"))

        self.assertEqual(redis.total_commands(), 50)
        self.assertEqual(redis.counts["set"], 20)
        self.assertEqual(redis.counts["expire"], 20)
        self.assertEqual(redis.counts["publish"], 10)

    def test_reproduced_legacy_trade_and_health_baselines_match_documented_table(self):
        keys = RedisKeyBuilder(prefix="")
        trade_redis = _SpyRedis()
        health_redis = _SpyRedis()

        for index in range(5):
            _legacy_write_trade(trade_redis, keys, _trade(trade_id=index))
            _legacy_write_health(health_redis, keys, _quote_envelope(index, symbol="AAPL"))

        self.assertEqual(trade_redis.total_commands(), 10)
        self.assertEqual(trade_redis.counts["hset"], 5)
        self.assertEqual(trade_redis.counts["expire"], 5)
        self.assertEqual(health_redis.total_commands(), 30)
        self.assertEqual(health_redis.counts["set"], 15)
        self.assertEqual(health_redis.counts["expire"], 15)

    def test_reproduced_legacy_order_flow_baseline_matches_documented_table(self):
        redis = _SpyRedis()
        keys = RedisKeyBuilder(prefix="")

        for index in range(8):
            _legacy_write_order_flow_bin(redis, keys, _trade(trade_id=index))

        self.assertEqual(redis.total_commands(), 16)
        self.assertEqual(redis.counts["hset"], 8)
        self.assertEqual(redis.counts["expire"], 8)


class _SpyRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.zsets = {}
        self.expirations = {}
        self.published = []
        self.commands = []
        self.counts = Counter()

    def _record(self, name, key=None):
        self.commands.append((name, key))
        self.counts[name] += 1

    def command_count(self, name, key):
        return sum(1 for command_name, command_key in self.commands if command_name == name and command_key == key)

    def total_commands(self):
        return len(self.commands)

    def set(self, key, value, nx=False, ex=None):
        self._record("set", key)
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def get(self, key):
        self._record("get", key)
        return self.values.get(key)

    def hset(self, key, *args, mapping=None, **kwargs):
        self._record("hset", key)
        values = dict(mapping or kwargs)
        if not values and len(args) >= 2:
            values = {args[0]: args[1]}
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def hgetall(self, key):
        self._record("hgetall", key)
        return dict(self.hashes.get(key, {}))

    def zadd(self, key, mapping):
        self._record("zadd", key)
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def expire(self, key, seconds):
        self._record("expire", key)
        self.expirations[key] = seconds
        return True

    def publish(self, channel, value):
        self._record("publish", channel)
        self.published.append((channel, value))
        return 1

    def delete(self, key):
        self._record("delete", key)
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.zsets.pop(key, None)
        return 1


class _Producer:
    def __init__(self):
        self.sent = []

    def send(self, topic, key, value):
        self.sent.append({"topic": topic, "key": key, "value": value})


def _accepted_feed_cache():
    return types.SimpleNamespace(get=lambda loader: {})


def _topics():
    return {
        "trades": "market.layer.trades.v1",
        "quotes": "market.layer.quotes.v1",
        "events": "market.layer.events.v1",
        "closed_candles": "market.layer.candles.closed.v1",
    }


def _quote_envelope(index, *, symbol):
    return build_raw_envelope({
        "T": "q",
        "S": symbol,
        "bp": round(100.0 + index * 0.01, 2),
        "bs": 10,
        "ap": round(100.1 + index * 0.01, 2),
        "as": 12,
        "t": f"2026-07-09T13:30:00.{index:03d}Z",
    }, "sip")


def _trade(*, symbol="NVDA", timestamp="2026-07-09T13:30:05.000Z", trade_id=1):
    return {
        "eventType": "TRADE",
        "symbol": symbol,
        "tradeId": trade_id,
        "price": 100.1,
        "size": 1,
        "timestamp": timestamp,
        "source": "alpaca",
        "feed": "sip",
        "feedProfile": "sip",
        "marketSession": "regular",
    }


def _legacy_write_quote(redis, keys, quote):
    key = keys.live_quote(quote["symbol"])
    redis.set(key, json.dumps(quote, ensure_ascii=False, separators=(",", ":")))
    redis.expire(key, 300)


def _legacy_write_trade(redis, keys, trade):
    key = keys.live_trade(trade["symbol"])
    redis.hset(key, mapping=trade)
    redis.expire(key, 300)


def _legacy_write_order_flow_bin(redis, keys, trade):
    key = keys.order_flow_live_minute(trade["symbol"])
    redis.hset(key, mapping={str(trade["tradeId"]): json.dumps(trade, separators=(",", ":"))})
    redis.expire(key, 86400)


def _legacy_publish_quote(redis, keys, quote):
    payload = json.dumps({"type": "QUOTE_UPDATE", "symbol": quote["symbol"], "data": quote}, separators=(",", ":"))
    redis.publish(keys.market_events_symbol(quote["symbol"]), payload)
    redis.publish(keys.market_events(), payload)


def _legacy_write_health(redis, keys, envelope):
    fields = {
        "lastChannel": envelope.get("channel"),
        "lastSymbol": envelope.get("symbol"),
        "lastFeedProfile": envelope.get("feedProfile"),
    }
    for component in (
        "market-processor",
        f"market-processor:symbol:{envelope['symbol']}",
        f"market-processor:feed:{envelope['feedProfile']}",
    ):
        key = keys.component_health(component)
        redis.set(key, json.dumps({"component": component, **fields}, separators=(",", ":")))
        redis.expire(key, 300)


if __name__ == "__main__":
    unittest.main()
