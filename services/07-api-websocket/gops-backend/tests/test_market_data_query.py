import sys
import types
import unittest
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PACKAGES = ROOT / "packages"
BACKEND = ROOT / "services" / "07-api-websocket" / "gops-backend"
for path in (str(PACKAGES), str(BACKEND)):
    if path not in sys.path:
        sys.path.insert(0, path)


try:
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    FASTAPI_TESTCLIENT_AVAILABLE = False

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def get(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

        def post(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    def Query(default=None, **kwargs):
        return default

    sys.modules["fastapi"] = types.SimpleNamespace(
        APIRouter=APIRouter,
        HTTPException=HTTPException,
        Query=Query,
        WebSocket=object,
        WebSocketDisconnect=Exception,
    )
    TestClient = None


try:
    import pydantic  # noqa: F401
except Exception:
    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self):
            return dict(self.__dict__)

    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=BaseModel)


sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

from app.market_data.query.service import MarketDataQueryService  # noqa: E402
from app.market_data.backfill.service import resolve_execution_mode  # noqa: E402
from app.market_data.query import routes as query_routes  # noqa: E402
from app.contracts.chart import AgentChatMessage, AgentChatRequest  # noqa: E402
from app.routes import charts as chart_routes  # noqa: E402
from app.services.ai_agents import fallback_agent_chat  # noqa: E402


class FakeProvider:
    def __init__(self, fail_snapshot=False):
        self.fail_snapshot = fail_snapshot
        self.last_limit = None

    def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None):
        self.last_limit = limit
        if self.fail_snapshot:
            raise RuntimeError("clickhouse unavailable")
        return {
            "symbol": symbol,
            "interval": interval,
            "snapshotCursor": "cursor-1",
            "candles": [{"timestamp": "2026-06-25T10:15:00.000Z", "close": 100}],
        }

    def search_symbols(self, query, limit):
        return [{"symbol": "AAPL", "name": "Apple Inc."}][:limit]

    def symbol_detail(self, symbol):
        if symbol == "ZZZZ":
            raise LookupError("Unknown market symbol: ZZZZ")
        return {"symbol": symbol, "name": symbol, "tradable": True}

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size):
        return {"symbol": symbol, "from": from_time, "to": to_time, "priceBinSize": 0.05, "bins": []}

    def latest_status(self, symbol=None):
        if symbol == "AAPL":
            return {"symbol": "AAPL", "statusType": "trading", "status": "active"}
        return None

    def agent_chart_context(self, symbol, interval, from_time, to_time, include):
        return {
            "symbol": symbol,
            "interval": interval,
            "visibleRange": {"from": from_time, "to": to_time},
            "include": sorted(include),
        }


class EmptyFakeProvider(FakeProvider):
    def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None):
        return {
            "symbol": symbol,
            "interval": interval,
            "source": "alpaca",
            "feed": "sip",
            "snapshotCursor": None,
            "candles": [],
        }


class FakeBackfillService:
    def snapshot_metadata(self, symbol, interval, payload_or_has_candles):
        has_candles = bool((payload_or_has_candles.get("candles") if isinstance(payload_or_has_candles, dict) else payload_or_has_candles))
        if has_candles:
            return {
                "dataStatus": "ready",
                "backfillStatus": "not_requested",
                "canBackfill": False,
                "message": None,
            }
        return {
            "dataStatus": "empty",
            "backfillStatus": "not_requested",
            "canBackfill": True,
            "message": f"No candle data is available for {symbol} {interval}.",
        }

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default"):
        return {
            "symbol": symbol,
            "interval": interval,
            "requestId": "backfill:INTC:1m:test",
            "status": "queued",
            "deduplicated": False,
        }

    def get_status(self, symbol, interval, request_id=None):
        return {
            "symbol": symbol,
            "interval": interval,
            "requestId": request_id or "backfill:INTC:1m:test",
            "status": "queued",
            "range": {"start": "2026-06-25T13:30:00.000Z", "end": "2026-06-25T14:30:00.000Z"},
        }


class FakeQueryService:
    def __init__(self, provider=None):
        self.service = MarketDataQueryService(provider or FakeProvider(), backfill_service=FakeBackfillService())

    def symbol_search(self, query, limit):
        return self.service.symbol_search(query, limit)

    def symbol_detail(self, symbol):
        return self.service.symbol_detail(symbol)

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size):
        return self.service.volume_profile_bins(symbol, from_time, to_time, price_bin_size)

    def latest_status(self, symbol=None):
        return self.service.latest_status(symbol)

    def agent_chart_context(self, symbol, interval, from_time, to_time, include):
        return self.service.agent_chart_context(symbol, interval, from_time, to_time, include)

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default"):
        return self.service.request_backfill(symbol, interval, start=start, end=end, mode=mode)

    def backfill_status(self, symbol, interval, request_id=None):
        return self.service.backfill_status(symbol, interval, request_id=request_id)


class MarketDataQueryServiceTest(unittest.TestCase):
    def test_candle_snapshot_adds_requested_indicators_and_normalizes_symbol(self):
        provider = FakeProvider()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        payload = service.candle_snapshot("aapl", "1m", "5,60,999", None)

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(provider.last_limit, 1440)
        self.assertEqual(payload["indicators"], {"ma": [5, 60], "volume": True})
        self.assertFalse(payload["isSynthetic"])

    def test_candle_snapshot_provider_error_maps_to_503(self):
        service = MarketDataQueryService(FakeProvider(fail_snapshot=True), backfill_service=FakeBackfillService())

        with self.assertRaises(HTTPException) as raised:
            service.candle_snapshot("AAPL", "1m", "5,20,60", 30)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("Market data provider failed", str(raised.exception.detail))

    def test_query_service_routes_core_market_context(self):
        service = MarketDataQueryService(FakeProvider(), backfill_service=FakeBackfillService())

        self.assertEqual(service.symbol_search("aa", 10)["symbols"][0]["symbol"], "AAPL")
        self.assertEqual(service.symbol_detail("aapl")["symbol"], "AAPL")
        self.assertEqual(service.latest_status("AAPL")["status"], "active")
        self.assertEqual(service.latest_status()["symbol"], "_MARKET")
        self.assertEqual(service.volume_profile_bins("aapl", "from", "to", "auto")["symbol"], "AAPL")
        context = service.agent_chart_context("aapl", "1m", "from", "to", "status,volumeProfile")
        self.assertEqual(context["include"], ["status", "volumeProfile"])

    def test_fallback_agent_analysis_request_returns_chart_command(self):
        request = AgentChatRequest(
            agentIds=["agent-01"],
            messages=[AgentChatMessage(role="user", content="이 차트를 분석해줘")],
            context={
                "chartDocument": {"symbol": "AAPL", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "123.45"},
            },
        )

        payload = fallback_agent_chat(request)

        self.assertTrue(payload["commands"])
        self.assertEqual(payload["commands"][0]["type"], "chart.drawing.add")

    def test_empty_candle_snapshot_includes_backfill_metadata(self):
        service = MarketDataQueryService(EmptyFakeProvider(), backfill_service=FakeBackfillService())

        payload = service.candle_snapshot("intc", "1m", "5,20,60", 30)

        self.assertEqual(payload["symbol"], "INTC")
        self.assertEqual(payload["candles"], [])
        self.assertEqual(payload["dataStatus"], "empty")
        self.assertEqual(payload["backfillStatus"], "not_requested")
        self.assertTrue(payload["canBackfill"])
        self.assertIn("No candle data", payload["message"])

    def test_backfill_routes_delegate_to_query_service(self):
        previous = chart_routes.get_query_service
        chart_routes.get_query_service = lambda: FakeQueryService(EmptyFakeProvider())
        try:
            requested = chart_routes.chart_backfill(chart_routes.BackfillRequestBody(
                symbol="intc",
                interval="1m",
                start="2026-06-25T13:30:00.000Z",
                end="2026-06-25T14:30:00.000Z",
            ))
            status = chart_routes.chart_backfill_status("intc", "1m")
        finally:
            chart_routes.get_query_service = previous

        self.assertEqual(requested["requestId"], "backfill:INTC:1m:test")
        self.assertEqual(status["status"], "queued")

    def test_requested_backfill_mode_is_ignored_unless_explicitly_enabled(self):
        previous_mode = os.environ.get("BACKFILL_EXECUTION_MODE")
        previous_allow = os.environ.get("BACKFILL_ALLOW_REQUESTED_MODE")
        os.environ["BACKFILL_EXECUTION_MODE"] = "queue"
        os.environ.pop("BACKFILL_ALLOW_REQUESTED_MODE", None)
        try:
            self.assertEqual(resolve_execution_mode("sample-dev"), "queue")
            os.environ["BACKFILL_ALLOW_REQUESTED_MODE"] = "true"
            self.assertEqual(resolve_execution_mode("sample-dev"), "sample-dev")
        finally:
            if previous_mode is None:
                os.environ.pop("BACKFILL_EXECUTION_MODE", None)
            else:
                os.environ["BACKFILL_EXECUTION_MODE"] = previous_mode
            if previous_allow is None:
                os.environ.pop("BACKFILL_ALLOW_REQUESTED_MODE", None)
            else:
                os.environ["BACKFILL_ALLOW_REQUESTED_MODE"] = previous_allow

    def test_symbol_detail_route_maps_unknown_symbol_to_404(self):
        previous = query_routes.get_query_service
        query_routes.get_query_service = lambda: FakeQueryService(FakeProvider())
        try:
            with self.assertRaises(HTTPException) as raised:
                query_routes.market_symbol_detail("ZZZZ")
        finally:
            query_routes.get_query_service = previous

        self.assertEqual(raised.exception.status_code, 404)

    @unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient dependency is not installed")
    def test_fastapi_symbol_search_route_with_testclient(self):
        from app.main import create_app

        previous = query_routes.get_query_service
        query_routes.get_query_service = lambda: FakeQueryService(FakeProvider())
        try:
            client = TestClient(create_app())
            response = client.get("/api/market/symbols/search?q=aa&limit=5")
        finally:
            query_routes.get_query_service = previous

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["symbols"][0]["symbol"], "AAPL")


if __name__ == "__main__":
    unittest.main()
