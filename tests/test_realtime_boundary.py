import asyncio
import sys
import types
import unittest
from pathlib import Path

class TestWebSocketDisconnect(Exception):
    def __init__(self, code=None):
        super().__init__(code)
        self.code = code


sys.modules.setdefault(
    "fastapi",
    types.SimpleNamespace(
        HTTPException=Exception,
        WebSocket=object,
        WebSocketDisconnect=TestWebSocketDisconnect,
    ),
)
sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "07-api-websocket" / "gops-backend"))

from app.market_data.realtime.session_manager import WebSocketSessionManager  # noqa: E402
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
    def refresh(self, symbol):
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
            manager.active_symbols = FakeActiveSymbols()
            manager.heartbeat_seconds = 999
            websocket = RaceWebSocket(hub)

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
            self.assertIn("CANDLE_CLOSED", event_types)
            self.assertIn("LIVE_CANDLE_UPDATE", event_types)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
