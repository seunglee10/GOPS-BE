import asyncio
import json
import unittest
from collections import Counter
from datetime import datetime, timezone

from market_data.common.redis_keys import RedisKeyBuilder
from market_data.serving.dto import websocket_event
from app.market_data.realtime.stream_hub import StreamSession, SymbolStreamHub


class MarketDataRealtimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_initial_snapshot_then_five_second_recovery_has_one_command_per_second_budget(self):
        redis = _Redis()
        keys = RedisKeyBuilder()
        now = _now_iso()
        redis.values[keys.live_candle("AAPL", "1m")] = json.dumps(_live_candle(now, 100.0))
        redis.hashes[keys.live_trade("AAPL")] = {"price": 100.0, "timestamp": now}
        redis.values[keys.live_quote("AAPL")] = json.dumps({"bidPrice": 99.9, "askPrice": 100.1, "timestamp": now})
        sleep = _OneRecoverySleep()
        hub = SymbolStreamHub(redis, _Provider(), sleep_fn=sleep)
        session = StreamSession("AAPL", "1m")
        hub.sessions_by_symbol["AAPL"] = {session}

        await hub._send_initial_snapshot(session)
        self.assertEqual(redis.commands, 5)
        self.assertEqual(session.queue.qsize(), 3)
        while not session.queue.empty():
            session.queue.get_nowait()

        redis.values[keys.live_candle("AAPL", "1m")] = json.dumps(_live_candle(now, 101.0, updated_at=_now_iso(1)))
        await hub._recover_missed_events()

        recovery_commands = redis.commands - 5
        self.assertEqual(sleep.elapsed, 5.0)
        self.assertEqual(recovery_commands, 5)
        self.assertLessEqual(recovery_commands / sleep.elapsed, 1.0)
        self.assertEqual(session.queue.qsize(), 1)

    async def test_pubsub_event_is_delivered_once_without_recovery(self):
        hub = SymbolStreamHub(_Redis(), _Provider())
        session = StreamSession("AAPL", "1m")
        hub.sessions_by_symbol["AAPL"] = {session}
        candle = _live_candle(_now_iso(), 102.0)
        event = websocket_event("LIVE_CANDLE_UPDATE", "AAPL", "1m", candle)

        await hub._broadcast_event(event)
        await hub._broadcast_event(event)

        self.assertEqual(session.queue.qsize(), 1)
        self.assertEqual((await session.queue.get())["data"]["close"], 102.0)

    async def test_snapshot_is_scoped_to_new_session_without_rebroadcasting_to_existing_session(self):
        redis = _Redis()
        keys = RedisKeyBuilder()
        now = _now_iso()
        redis.values[keys.live_candle("AAPL", "1m")] = json.dumps(_live_candle(now, 100.0))
        hub = SymbolStreamHub(redis, _Provider())
        first = StreamSession("AAPL", "1m")
        second = StreamSession("AAPL", "1m")
        hub.sessions_by_symbol["AAPL"] = {first, second}

        await hub._send_initial_snapshot(second)

        self.assertEqual(first.queue.qsize(), 0)
        self.assertEqual(second.queue.qsize(), 1)


class _OneRecoverySleep:
    def __init__(self):
        self.calls = 0
        self.elapsed = 0.0

    async def __call__(self, seconds):
        self.calls += 1
        if self.calls > 1:
            raise asyncio.CancelledError
        self.elapsed += seconds


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    def get(self, key):
        self.operations.append(("get", key))
        self.redis.counts["get"] += 1
        self.redis.commands += 1
        return self

    def hgetall(self, key):
        self.operations.append(("hgetall", key))
        self.redis.counts["hgetall"] += 1
        self.redis.commands += 1
        return self

    def execute(self):
        return [
            self.redis.values.get(key) if operation == "get" else dict(self.redis.hashes.get(key, {}))
            for operation, key in self.operations
        ]


class _Redis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.commands = 0
        self.counts = Counter()

    def pipeline(self):
        return _Pipeline(self)


class _Provider:
    class _RedisProvider:
        @staticmethod
        def live_event(_symbol, _interval):
            return None

        @staticmethod
        def live_trade(_symbol):
            return None

        @staticmethod
        def live_quote(_symbol):
            return None

    redis_provider = _RedisProvider()


def _now_iso(offset_seconds=0):
    value = datetime.now(timezone.utc).timestamp() + offset_seconds
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _live_candle(timestamp, close, updated_at=None):
    return {
        "eventType": "LIVE_CANDLE",
        "symbol": "AAPL",
        "interval": "1m",
        "timestamp": timestamp,
        "open": 100.0,
        "high": max(100.0, close),
        "low": min(100.0, close),
        "close": close,
        "volume": 10,
        "isClosed": False,
        "source": "alpaca.trades",
        "updatedAt": updated_at or timestamp,
    }


if __name__ == "__main__":
    unittest.main()
