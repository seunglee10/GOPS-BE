import asyncio
import os
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT / "systems" / "market-data" / "shared",
    ROOT / "systems" / "order" / "shared",
    ROOT / "systems" / "order",
    ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

from fastapi import HTTPException

from app.market_data.query.service import MarketDataQueryService
from app.market_data.realtime.stream_hub import StreamSession, should_deliver_to_session
from app.routes.streams import _serve_simulation_chart
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider


class OrderFlowQueryServiceTest(unittest.TestCase):
    def test_symbols_endpoint_payload_uses_env_pins(self):
        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "nvda,aapl", "ORDER_FLOW_PRICE_BIN_SIZE": "0.01"}):
            payload = MarketDataQueryService(provider=FakeProvider()).order_flow_symbols()

        self.assertEqual(payload["symbols"], ["AAPL", "NVDA"])
        self.assertEqual(payload["priceBinSize"], 0.01)
        self.assertEqual(payload["sideClassification"], "estimated")

    def test_daily_groups_rows_totals_caps_days_and_provider_uses_final(self):
        clickhouse = CapturingClickHouseProvider([
            row("2026-07-08", 102.0, ask=8, bid=3, unknown=2, ask_count=2, bid_count=1, unknown_count=1),
            row("2026-07-07", 101.0, ask=5, bid=7, unknown=0, ask_count=1, bid_count=2, unknown_count=0),
            row("2026-07-06", 100.0, ask=10, bid=4, unknown=1, ask_count=2, bid_count=1, unknown_count=1),
        ])
        service = MarketDataQueryService(provider=FakeProvider(clickhouse_provider=clickhouse))

        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            payload = service.order_flow_daily("nvda", "2026-07-01", "2026-07-09", limit_days=2)

        self.assertEqual(payload["dataStatus"], "ready")
        self.assertEqual([day["sessionDate"] for day in payload["days"]], ["2026-07-07", "2026-07-08"])
        self.assertEqual(payload["days"][1]["totals"]["delta"], 5)
        self.assertEqual(payload["days"][1]["totals"]["volume"], 13)
        self.assertIn("FINAL", clickhouse.last_query)
        self.assertIn("ORDER BY session_date DESC, price_bin ASC", clickhouse.last_query)

    def test_daily_wide_range_keeps_most_recent_complete_days(self):
        rows = (
            [row("2026-07-08", 100 + i * 0.01, ask=1, ask_count=1) for i in range(5000)]
            + [row("2026-07-07", 90 + i * 0.01, bid=1, bid_count=1) for i in range(5000)]
            + [row("2026-07-06", 80.0, unknown=1, unknown_count=1)]
        )
        clickhouse = CapturingClickHouseProvider(rows)
        service = MarketDataQueryService(provider=FakeProvider(clickhouse_provider=clickhouse))

        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            payload = service.order_flow_daily("NVDA", "2026-01-01", "2026-07-09", limit_days=2)

        self.assertEqual([day["sessionDate"] for day in payload["days"]], ["2026-07-07", "2026-07-08"])
        self.assertEqual(len(payload["days"][0]["levels"]), 5000)
        self.assertEqual(len(payload["days"][1]["levels"]), 5000)

    def test_daily_unsupported_and_empty_shapes(self):
        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            unsupported = MarketDataQueryService(provider=FakeProvider()).order_flow_daily("MSFT", "2026-07-01", "2026-07-09")
            empty = MarketDataQueryService(provider=FakeProvider(clickhouse_provider=CapturingClickHouseProvider([]))).order_flow_daily("NVDA", "2026-07-01", "2026-07-09")

        self.assertEqual(unsupported["dataStatus"], "unsupported")
        self.assertEqual(unsupported["days"], [])
        self.assertEqual(empty["dataStatus"], "empty")
        self.assertEqual(empty["days"], [])

    def test_intraday_filters_stale_session_and_passes_live_quote(self):
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        redis_provider = FakeRedisProvider(
            bins=[
                live_bin("2026-07-09T13:30:00.000Z", today, 100.0, ask=10),
                live_bin("2026-07-08T13:30:00.000Z", "2026-07-08", 99.0, bid=9),
            ],
            quote={"bidPrice": 100.1, "askPrice": 100.2, "timestamp": "2026-07-09T13:30:03.000Z"},
        )
        service = MarketDataQueryService(provider=FakeProvider(redis_provider=redis_provider))

        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            payload = service.order_flow_intraday("NVDA")

        self.assertEqual(payload["dataStatus"], "ready")
        self.assertEqual(len(payload["minutes"]), 1)
        self.assertEqual(payload["minutes"][0]["bins"][0]["askVolume"], 10)
        self.assertEqual(payload["liveQuote"]["bidPrice"], 100.1)

    def test_intraday_empty_and_unsupported(self):
        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            empty = MarketDataQueryService(provider=FakeProvider(redis_provider=FakeRedisProvider())).order_flow_intraday("NVDA")
            unsupported = MarketDataQueryService(provider=FakeProvider(redis_provider=FakeRedisProvider())).order_flow_intraday("MSFT")

        self.assertEqual(empty["dataStatus"], "empty")
        self.assertEqual(empty["minutes"], [])
        self.assertEqual(unsupported["dataStatus"], "unsupported")
        self.assertEqual(unsupported["minutes"], [])

    def test_provider_failures_map_to_503(self):
        with mock.patch.dict(os.environ, {"ORDER_FLOW_PINNED_SYMBOLS": "NVDA"}):
            with self.assertRaises(HTTPException) as daily_ctx:
                MarketDataQueryService(provider=FakeProvider(clickhouse_provider=FailingClickHouseProvider())).order_flow_daily("NVDA", "2026-07-01", "2026-07-09")
            with self.assertRaises(HTTPException) as intraday_ctx:
                MarketDataQueryService(provider=FakeProvider(redis_provider=FailingRedisProvider())).order_flow_intraday("NVDA")

        self.assertEqual(daily_ctx.exception.status_code, 503)
        self.assertIn("Market data provider failed:", daily_ctx.exception.detail)
        self.assertEqual(intraday_ctx.exception.status_code, 503)


class OrderFlowStreamHubTest(unittest.IsolatedAsyncioTestCase):
    async def test_order_flow_delivery_is_symbol_matched_interval_agnostic(self):
        event = {"type": "ORDER_FLOW_BINS_UPDATE", "symbol": "NVDA", "interval": "1m", "data": {}}

        self.assertTrue(should_deliver_to_session(event, StreamSession("NVDA", "1D")))
        self.assertTrue(should_deliver_to_session(event, StreamSession("NVDA", "1m")))
        self.assertFalse(should_deliver_to_session(event, StreamSession("AAPL", "1m")))

    async def test_order_flow_event_is_droppable_under_backpressure(self):
        session = StreamSession("NVDA", "1m", queue_size=1)
        await session.enqueue({"type": "ORDER_FLOW_BINS_UPDATE", "symbol": "NVDA", "data": {"eventMinute": "old"}})
        await session.enqueue({"type": "ORDER_FLOW_BINS_UPDATE", "symbol": "NVDA", "data": {"eventMinute": "new"}})

        item = session.queue.get_nowait()
        self.assertEqual(item["data"]["eventMinute"], "new")
        self.assertNotEqual(item["type"], "ERROR")

    async def test_simulation_socket_opt_in_emits_replay_order_flow_and_quote(self):
        gateway = FakeSimulationStreamGateway()
        websocket = FakeSimulationWebSocket(gateway)

        with (
            mock.patch("app.routes.streams.simulator_mode_active", side_effect=[True, False]),
            mock.patch("app.routes.streams.asyncio.sleep", new=mock.AsyncMock()) as sleep,
        ):
            await _serve_simulation_chart(websocket, "NVDA", "1m", None, order_flow=True)

        events = {event["type"]: event for event in websocket.sent}
        self.assertIn("ORDER_FLOW_BINS_UPDATE", events)
        self.assertIn("LIVE_QUOTE_UPDATE", events)
        self.assertTrue(events["ORDER_FLOW_BINS_UPDATE"]["simulation"])
        self.assertEqual(events["ORDER_FLOW_BINS_UPDATE"]["runId"], "run-1")
        self.assertEqual(events["ORDER_FLOW_BINS_UPDATE"]["data"]["sessionDate"], "2026-07-14")
        self.assertEqual(events["LIVE_QUOTE_UPDATE"]["data"]["bidPrice"], 100.0)
        self.assertEqual(websocket.closed_code, 1012)
        sleep.assert_awaited_once_with(0.25)

    async def test_order_flow_only_simulation_socket_never_reads_candles(self):
        gateway = FakeSimulationStreamGateway()
        websocket = FakeSimulationWebSocket(gateway)

        with (
            mock.patch("app.routes.streams.simulator_mode_active", side_effect=[True, False]),
            mock.patch("app.routes.streams.asyncio.sleep", new=mock.AsyncMock()) as sleep,
        ):
            await _serve_simulation_chart(
                websocket,
                "NVDA",
                "1m",
                None,
                order_flow=True,
                candles=False,
            )

        self.assertEqual(gateway.candle_calls, 0)
        self.assertEqual(gateway.order_flow_calls, 1)
        self.assertIn("ORDER_FLOW_BINS_UPDATE", {event["type"] for event in websocket.sent})
        sleep.assert_awaited_once_with(1.0)


class FakeSimulationStreamGateway:
    def __init__(self):
        self.candle_calls = 0
        self.order_flow_calls = 0

    def candles(self, symbol, interval, limit):
        self.candle_calls += 1
        return {"candles": []}

    def order_flow(self, symbol, **_kwargs):
        self.order_flow_calls += 1
        return {
            "runId": "run-1",
            "nextSequence": 42,
            "datasetId": "dataset-1",
            "virtualTime": "2026-07-14T15:00:30.000Z",
            "sessionDate": "2026-07-14",
            "priceBinSize": 0.01,
            "minutes": [{
                "eventMinute": "2026-07-14T15:00:00Z",
                "updatedAt": "2026-07-14T15:00:20.000Z",
                "bins": [{"priceBin": 100.0, "askVolume": 2, "bidVolume": 1, "unknownVolume": 0}],
            }],
            "liveQuote": {
                "bidPrice": 100.0,
                "askPrice": 100.1,
                "timestamp": "2026-07-14T15:00:25.000Z",
            },
        }


class FakeSimulationWebSocket:
    def __init__(self, gateway):
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(simulator_gateway=gateway))
        self.sent = []
        self.closed_code = None

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code):
        self.closed_code = code


class CapturingClickHouseProvider:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = ""
        self.last_parameters = None

    def order_flow_daily_profiles(self, symbol, from_date, to_date, limit=100000):
        provider = ClickHouseMarketDataProvider.__new__(ClickHouseMarketDataProvider)
        provider.database = "market_data"

        def capture(query, parameters):
            self.last_query = query
            self.last_parameters = parameters
            return self.rows

        provider.query_json_each_row = capture
        return provider.order_flow_daily_profiles(symbol, from_date, to_date, limit=limit)


class FailingClickHouseProvider:
    def order_flow_daily_profiles(self, symbol, from_date, to_date, limit=100000):
        raise RuntimeError("clickhouse unavailable")


class FakeRedisProvider:
    def __init__(self, bins=None, quote=None):
        self.bins = bins or []
        self.quote = quote

    def order_flow_live_bins(self, symbol):
        return list(self.bins)

    def live_quote(self, symbol):
        return self.quote


class FailingRedisProvider:
    def order_flow_live_bins(self, symbol):
        raise RuntimeError("redis unavailable")


class FakeProvider:
    def __init__(self, clickhouse_provider=None, redis_provider=None):
        self.clickhouse_provider = clickhouse_provider or CapturingClickHouseProvider([])
        self.redis_provider = redis_provider or FakeRedisProvider()


def row(session_date, price, *, ask=0, bid=0, unknown=0, ask_count=0, bid_count=0, unknown_count=0):
    return {
        "sessionDate": session_date,
        "priceBin": price,
        "priceBinSize": 0.01,
        "askVolume": ask,
        "bidVolume": bid,
        "unknownVolume": unknown,
        "askTradeCount": ask_count,
        "bidTradeCount": bid_count,
        "unknownTradeCount": unknown_count,
        "tradeCount": ask_count + bid_count + unknown_count,
        "volume": ask + bid + unknown,
    }


def live_bin(minute, session_date, price, *, ask=0, bid=0, unknown=0):
    return {
        "eventMinute": minute,
        "sessionDate": session_date,
        "priceBin": price,
        "askVolume": ask,
        "bidVolume": bid,
        "unknownVolume": unknown,
        "askTradeCount": 1 if ask else 0,
        "bidTradeCount": 1 if bid else 0,
        "unknownTradeCount": 1 if unknown else 0,
    }


if __name__ == "__main__":
    unittest.main()
