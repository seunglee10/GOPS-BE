import asyncio
import sys
import types
import unittest
from pathlib import Path


try:
    from fastapi import WebSocketDisconnect as BaseWebSocketDisconnect
except Exception:
    BaseWebSocketDisconnect = Exception
    sys.modules.setdefault(
        "fastapi",
        types.SimpleNamespace(
            HTTPException=Exception,
            WebSocket=object,
            WebSocketDisconnect=BaseWebSocketDisconnect,
        ),
    )


class TestWebSocketDisconnect(BaseWebSocketDisconnect):
    def __init__(self, code=None):
        super().__init__(code)
        self.code = code


if BaseWebSocketDisconnect is Exception:
    sys.modules["fastapi"].WebSocketDisconnect = TestWebSocketDisconnect

sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "market-data" / "shared"))
sys.path.insert(0, str(ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"))

from app.market_data.realtime.session_manager import WebSocketSessionManager, quote_stream_period_seconds  # noqa: E402
from app.market_data.realtime.stream_hub import StreamSession, SymbolStreamHub  # noqa: E402
from alfaka.serving.cursors import timestamp_from_cursor  # noqa: E402

WebSocketDisconnect = TestWebSocketDisconnect


class TestableHub(SymbolStreamHub):
    async def _listen_symbol(self, symbol):
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            return


class FakeActiveSymbols:
    def __init__(self):
        self.refreshed = []
        self.on_refresh = None

    def refresh(self, symbol):
        self.refreshed.append(symbol)
        if self.on_refresh:
            self.on_refresh(symbol)
        return None


class FakeProvider:
    def __init__(self):
        self.redis_provider = type("RedisProvider", (), {"redis": None})()

    def candles_since_cursor(self, symbol, interval, cursor):
        return [{
            "timestamp": "2026-06-25T10:15:00.000Z",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1000,
            "isClosed": True,
            "sourceEventId": "gap-fill",
        }]


class RaceHub:
    def __init__(self):
        self.session = None
        self.broadcast_attempted = False
        self.unsubscribed = False
        self.live_event = {
            "type": "LIVE_CANDLE_UPDATE",
            "eventId": "live-during-gap-fill",
            "cursor": "cursor-live",
            "symbol": "AAPL",
            "interval": "1m",
            "data": {"timestamp": "2026-06-25T10:15:30.000Z", "close": 101.0},
        }

    async def subscribe(self, session):
        self.session = session

    async def unsubscribe(self, session):
        self.unsubscribed = True

    async def broadcast_during_gap_fill(self):
        self.broadcast_attempted = True
        if self.session:
            await self.session.enqueue(self.live_event)


class RaceWebSocket:
    def __init__(self, hub):
        self.hub = hub
        self.sent = []
        self.accepted = False
        self.injected = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, event):
        self.sent.append(event)
        if event.get("type") == "CANDLE_CLOSED" and not self.injected:
            self.injected = True
            await self.hub.broadcast_during_gap_fill()
            return
        if event.get("type") == "LIVE_CANDLE_UPDATE":
            raise WebSocketDisconnect(code=1000)


class QuoteRedisProvider:
    redis = None

    def __init__(self):
        self.loop_count = 0
        self.live_calls = []
        self.events = {
            "AAPL": {
                "type": "LIVE_CANDLE_UPDATE",
                "eventId": "quote-aapl-1",
                "cursor": "cursor-aapl-1",
                "symbol": "AAPL",
                "interval": "1m",
                "data": {
                    "timestamp": "2026-06-25T10:15:00.000Z",
                    "close": 100.0,
                    "volume": 10,
                },
            },
            "MSFT": {
                "type": "LIVE_CANDLE_UPDATE",
                "eventId": "quote-msft-1",
                "cursor": "cursor-msft-1",
                "symbol": "MSFT",
                "interval": "1m",
                "data": {
                    "timestamp": "2026-06-25T10:15:00.000Z",
                    "close": 200.0,
                    "volume": 20,
                },
            },
        }

    def live_event(self, symbol, interval):
        self.live_calls.append((symbol, interval))
        if symbol == "AAPL":
            self.loop_count += 1
            if self.loop_count > 2:
                raise WebSocketDisconnect(code=1000)
        return self.events.get(symbol)

    def closed_event(self, symbol, interval):
        return None


class QuoteWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, event):
        self.sent.append(event)


class RealtimeBoundaryTest(unittest.TestCase):
    def test_timestamp_from_cursor_preserves_iso_colons(self):
        cursor = "v1:AAPL:1m:2026-06-25T10:15:00.000Z:abc123"
        self.assertEqual(timestamp_from_cursor(cursor), "2026-06-25T10:15:00.000Z")

    def test_same_symbol_sessions_share_one_hub_task(self):
        async def run():
            hub = TestableHub(redis_client=None, provider=None)
            first = StreamSession("AAPL", "1m")
            second = StreamSession("AAPL", "1m")
            await hub.subscribe(first)
            await hub.subscribe(second)
            self.assertEqual(len(hub.tasks), 1)
            self.assertEqual(len(hub.sessions_by_symbol["AAPL"]), 2)
            await hub.unsubscribe(first)
            self.assertEqual(len(hub.tasks), 1)
            await hub.unsubscribe(second)
            self.assertEqual(len(hub.tasks), 0)

        asyncio.run(run())

    def test_broadcast_filters_by_symbol_and_interval(self):
        async def run():
            hub = TestableHub(redis_client=None, provider=None)
            aapl_1m = StreamSession("AAPL", "1m")
            aapl_5m = StreamSession("AAPL", "5m")
            hub.sessions_by_symbol["AAPL"] = {aapl_1m, aapl_5m}
            await hub._broadcast("AAPL", {
                "type": "CANDLE_CLOSED",
                "symbol": "AAPL",
                "interval": "1m",
                "data": {"timestamp": "2026-06-25T10:15:00.000Z"},
            })
            self.assertEqual(aapl_1m.queue.qsize(), 1)
            self.assertEqual(aapl_5m.queue.qsize(), 0)

        asyncio.run(run())

    def test_redis_live_fallback_reads_each_subscribed_interval(self):
        async def run():
            class FakeRedisProvider:
                redis = None

                def live_event(self, symbol, interval="1m"):
                    if symbol == "AAPL" and interval == "5m":
                        return {
                            "type": "LIVE_CANDLE_UPDATE",
                            "eventId": "live-5m",
                            "cursor": "cursor-5m",
                            "symbol": "AAPL",
                            "interval": "5m",
                            "data": {
                                "timestamp": "2026-06-25T10:15:00.000Z",
                                "open": 100,
                                "high": 101,
                                "low": 99,
                                "close": 100.5,
                                "volume": 10,
                                "isClosed": False,
                            },
                        }
                    return None

            provider = types.SimpleNamespace(redis_provider=FakeRedisProvider())
            hub = TestableHub(redis_client=None, provider=provider)
            aapl_1m = StreamSession("AAPL", "1m")
            aapl_5m = StreamSession("AAPL", "5m")
            hub.sessions_by_symbol["AAPL"] = {aapl_1m, aapl_5m}

            await hub._broadcast_latest_redis_live_event("AAPL")

            self.assertEqual(aapl_1m.queue.qsize(), 0)
            self.assertEqual(aapl_5m.queue.qsize(), 1)
            event = await aapl_5m.queue.get()
            self.assertEqual(event["interval"], "5m")

        asyncio.run(run())

    def test_market_status_delivers_to_symbol_session_without_interval_match(self):
        async def run():
            hub = TestableHub(redis_client=None, provider=None)
            aapl_1m = StreamSession("AAPL", "1m")
            hub.sessions_by_symbol["AAPL"] = {aapl_1m}
            await hub._broadcast("AAPL", {
                "type": "MARKET_STATUS_UPDATE",
                "eventId": "status-1",
                "symbol": "_MARKET",
                "interval": "status",
                "data": {"eventTime": "2026-06-25T13:30:00.000Z"},
            })
            self.assertEqual(aapl_1m.queue.qsize(), 1)

        asyncio.run(run())

    def test_duplicate_pubsub_event_is_not_enqueued_twice(self):
        async def run():
            hub = TestableHub(redis_client=None, provider=None)
            aapl_1m = StreamSession("AAPL", "1m")
            hub.sessions_by_symbol["AAPL"] = {aapl_1m}
            event = {
                "type": "CANDLE_CLOSED",
                "eventId": "event-1",
                "cursor": "cursor-1",
                "symbol": "AAPL",
                "interval": "1m",
                "data": {"timestamp": "2026-06-25T10:15:00.000Z"},
            }
            await hub._broadcast("AAPL", event)
            await hub._broadcast("AAPL", event)
            self.assertEqual(aapl_1m.queue.qsize(), 1)

        asyncio.run(run())

    def test_slow_client_drops_live_update_before_closed_candle(self):
        async def run():
            session = StreamSession("AAPL", "1m", queue_size=2)
            await session.enqueue({"type": "LIVE_CANDLE_UPDATE", "symbol": "AAPL", "interval": "1m", "data": {"timestamp": "t1"}})
            await session.enqueue({"type": "LIVE_CANDLE_UPDATE", "symbol": "AAPL", "interval": "1m", "data": {"timestamp": "t2"}})
            await session.enqueue({"type": "CANDLE_CLOSED", "symbol": "AAPL", "interval": "1m", "data": {"timestamp": "t3"}})
            queued = [await session.queue.get(), await session.queue.get()]
            self.assertEqual([item["type"] for item in queued], ["LIVE_CANDLE_UPDATE", "CANDLE_CLOSED"])
            self.assertEqual(queued[-1]["data"]["timestamp"], "t3")

        asyncio.run(run())

    def test_session_subscribes_before_gap_fill_to_avoid_lost_delta(self):
        async def run():
            hub = RaceHub()
            manager = WebSocketSessionManager(provider=FakeProvider())
            manager.hub = hub
            active_symbols = FakeActiveSymbols()
            ordering = []
            active_symbols.on_refresh = lambda symbol: ordering.append(("refresh", symbol))
            manager.active_symbols = active_symbols
            manager.heartbeat_seconds = 999
            websocket = RaceWebSocket(hub)
            original_send_gap_fill = manager._send_gap_fill

            async def send_gap_fill_with_marker(*args, **kwargs):
                ordering.append(("gap-fill", args[1]))
                await original_send_gap_fill(*args, **kwargs)

            manager._send_gap_fill = send_gap_fill_with_marker

            try:
                await asyncio.wait_for(
                    manager.serve_chart(websocket, "AAPL", "1m", cursor="v1:AAPL:1m:2026-06-25T10:14:00.000Z:abc"),
                    timeout=0.2,
                )
            except asyncio.TimeoutError:
                pass

            event_types = [event.get("type") for event in websocket.sent]
            self.assertTrue(websocket.accepted)
            self.assertTrue(hub.broadcast_attempted)
            self.assertTrue(hub.unsubscribed)
            self.assertEqual(ordering[:2], [("refresh", "AAPL"), ("gap-fill", "AAPL")])
            self.assertIn("CANDLE_CLOSED", event_types)
            self.assertIn("LIVE_CANDLE_UPDATE", event_types)

        asyncio.run(run())

    def test_quote_stream_batches_symbols_and_dedupes_unchanged_events(self):
        async def run():
            redis_provider = QuoteRedisProvider()
            provider = types.SimpleNamespace(redis_provider=redis_provider)
            active_symbols = FakeActiveSymbols()
            manager = WebSocketSessionManager(provider=provider)
            manager.active_symbols = active_symbols
            manager.heartbeat_seconds = 10**12
            websocket = QuoteWebSocket()

            await asyncio.wait_for(
                manager.serve_quotes(websocket, ["AAPL", "MSFT"], "1m", max_hz=4.0),
                timeout=1.0,
            )

            self.assertTrue(websocket.accepted)
            self.assertEqual(
                [(event["symbol"], event["interval"], event["eventId"]) for event in websocket.sent],
                [("AAPL", "1m", "quote-aapl-1"), ("MSFT", "1m", "quote-msft-1")],
            )
            self.assertEqual(active_symbols.refreshed, [])
            self.assertGreaterEqual(redis_provider.live_calls.count(("AAPL", "1m")), 2)
            self.assertGreaterEqual(redis_provider.live_calls.count(("MSFT", "1m")), 2)

        asyncio.run(run())

    def test_quote_stream_period_defaults_to_one_hz_and_clamps_edges(self):
        self.assertEqual(quote_stream_period_seconds(1.0), 1.0)
        self.assertEqual(quote_stream_period_seconds(None), 1.0)
        self.assertEqual(quote_stream_period_seconds(0), 1.0)
        self.assertEqual(quote_stream_period_seconds(10), 0.25)
        self.assertEqual(quote_stream_period_seconds(0.05), 5.0)


if __name__ == "__main__":
    unittest.main()
