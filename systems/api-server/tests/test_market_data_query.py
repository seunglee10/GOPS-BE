import sys
import types
import unittest
import os
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND)):
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
from app.market_data.backfill.service import BackfillService, resolve_execution_mode  # noqa: E402
from app.market_data.query import routes as query_routes  # noqa: E402
from app.contracts.chart import AgentChatMessage, AgentChatRequest  # noqa: E402
from app.routes import charts as chart_routes  # noqa: E402
from app.routes.health import runtime_config  # noqa: E402
from app.services.alfaka_market_data import configured_symbols  # noqa: E402
from app.services.ai_agents import build_agent_market_analysis_context, chart_context_for_agent_prompt, is_live_feed_status_request, openai_agent_chat  # noqa: E402


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
            "sourceInterval": interval,
            "message": f"No stored {interval} candles were found for {symbol}.",
            "coverage": {
                "state": "empty",
                "reasonCode": "no_stored_candles",
                "sourceInterval": interval,
                "backfillStatus": "not_requested",
                "returnedCount": 0,
            },
        }

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default", force=False):
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


class RecordingBackfillStore:
    def __init__(self):
        self.created = []
        self.latest = {}

    def create_request(self, symbol, interval, start=None, end=None, mode="default", force=False):
        self.created.append((symbol, interval, start, end, mode, force))
        record = {
            "symbol": symbol,
            "interval": interval,
            "requestId": f"backfill:{symbol}:{interval}:test",
            "status": "queued",
            "range": {"start": start or "auto-start", "end": end or "auto-end"},
            "requestedAt": "2026-06-25T13:30:00.000Z",
            "updatedAt": "2026-06-25T13:30:00.000Z",
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "result": None,
        }
        self.latest[(symbol, interval)] = record
        return record, False

    def latest_status(self, *args, **kwargs):
        return self.latest.get(tuple(args))

    def get_status(self, request_id):
        for record in self.latest.values():
            if record["requestId"] == request_id:
                return record
        return None


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

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default", force=False):
        return self.service.request_backfill(symbol, interval, start=start, end=end, mode=mode, force=force)

    def backfill_status(self, symbol, interval, request_id=None):
        return self.service.backfill_status(symbol, interval, request_id=request_id)


class MarketDataQueryServiceTest(unittest.TestCase):
    def test_candle_snapshot_adds_requested_indicators_and_normalizes_symbol(self):
        provider = FakeProvider()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        payload = service.candle_snapshot("aapl", "1m", "5,60,999", None)

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(provider.last_limit, 390)
        self.assertEqual(payload["indicators"], {"ma": [5, 60], "volume": True})

    def test_candle_snapshot_accepts_canonical_and_legacy_intervals(self):
        provider = FakeProvider()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        daily_payload = service.candle_snapshot("aapl", "1d", "5,20,60", None)
        weekly_payload = service.candle_snapshot("aapl", "1W", "5,20,60", None)
        monthly_payload = service.candle_snapshot("aapl", "1M", "5,20,60", None)

        self.assertEqual(daily_payload["interval"], "1D")
        self.assertEqual(weekly_payload["interval"], "1W")
        self.assertEqual(monthly_payload["interval"], "1M")

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

    def test_agent_chat_without_openai_key_returns_503(self):
        request = AgentChatRequest(
            agentIds=["agent-01"],
            messages=[AgentChatMessage(role="user", content="이 차트를 분석해줘")],
            context={
                "chartDocument": {"symbol": "AAPL", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "123.45"},
            },
        )

        with mock.patch("app.services.ai_agents.read_dotenv_value", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                openai_agent_chat(request)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("OpenAI API key is not configured", str(raised.exception.detail))

    def test_agent_market_context_separates_chart_data_from_live_stream_status(self):
        previous = os.environ.get("ALPACA_SYMBOLS")
        os.environ["ALPACA_SYMBOLS"] = "NVDA,AMD"
        try:
            context = build_agent_market_analysis_context({
                "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
                "visibleSummary": {"lastPrice": "123.45", "high": "125.00", "low": "120.00"},
                "dataStatus": {"state": "ready", "candleCount": 120, "backfillStatus": "not_requested"},
                "streamStatus": "error",
            })
        finally:
            if previous is None:
                os.environ.pop("ALPACA_SYMBOLS", None)
            else:
                os.environ["ALPACA_SYMBOLS"] = previous

        self.assertEqual(context["symbol"], "NVDA")
        self.assertTrue(context["dataReadiness"]["hasUsableCandles"])
        self.assertEqual(context["dataReadiness"]["candleCount"], 120)
        self.assertEqual(context["dataReadiness"]["liveFeedStatus"], "error")

    def test_agent_prompt_omits_live_status_for_general_chart_analysis(self):
        analysis_request = AgentChatRequest(
            agentIds=["agent-01"],
            messages=[AgentChatMessage(role="user", content="차트를 분석해줘")],
            context={},
        )
        live_status_request = AgentChatRequest(
            agentIds=["agent-01"],
            messages=[AgentChatMessage(role="user", content="실시간 연결 상태를 확인해줘")],
            context={},
        )
        self.assertFalse(is_live_feed_status_request(analysis_request))
        self.assertTrue(is_live_feed_status_request(live_status_request))

        prompt_context = chart_context_for_agent_prompt({
            "chartDocument": {"symbol": "NVDA", "timeframe": "1m"},
            "dataStatus": {"state": "ready", "candleCount": 120},
            "streamStatus": "error",
        }, include_live_status=False)
        self.assertNotIn("streamStatus", prompt_context)
        self.assertTrue(build_agent_market_analysis_context(prompt_context)["dataReadiness"]["hasUsableCandles"])
        self.assertNotIn("liveFeedStatus", build_agent_market_analysis_context(prompt_context)["dataReadiness"])

    def test_empty_candle_snapshot_includes_backfill_metadata(self):
        service = MarketDataQueryService(EmptyFakeProvider(), backfill_service=FakeBackfillService())

        payload = service.candle_snapshot("intc", "1m", "5,20,60", 30)

        self.assertEqual(payload["symbol"], "INTC")
        self.assertEqual(payload["candles"], [])
        self.assertEqual(payload["dataStatus"], "empty")
        self.assertEqual(payload["backfillStatus"], "not_requested")
        self.assertTrue(payload["canBackfill"])
        self.assertIn("No stored 1m candles", payload["message"])
        self.assertEqual(payload["coverage"]["state"], "empty")
        self.assertEqual(payload["coverage"]["reasonCode"], "no_stored_candles")

    def test_configured_symbols_uses_alpaca_symbols_watchlist_seed(self):
        previous = os.environ.get("ALPACA_SYMBOLS")
        os.environ["ALPACA_SYMBOLS"] = "NVDA,AMD,AVGO,TSM,ASML,AMAT,MU"
        try:
            self.assertEqual(configured_symbols(), ["NVDA", "AMD", "AVGO", "TSM", "ASML", "AMAT", "MU"])
        finally:
            if previous is None:
                os.environ.pop("ALPACA_SYMBOLS", None)
            else:
                os.environ["ALPACA_SYMBOLS"] = previous

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
            self.assertEqual(resolve_execution_mode("unsupported-dev"), "queue")
            os.environ["BACKFILL_ALLOW_REQUESTED_MODE"] = "true"
            self.assertEqual(resolve_execution_mode("unsupported-dev"), "queue")
            self.assertEqual(resolve_execution_mode("sync-dev"), "sync-dev")
        finally:
            if previous_mode is None:
                os.environ.pop("BACKFILL_EXECUTION_MODE", None)
            else:
                os.environ["BACKFILL_EXECUTION_MODE"] = previous_mode
            if previous_allow is None:
                os.environ.pop("BACKFILL_ALLOW_REQUESTED_MODE", None)
            else:
                os.environ["BACKFILL_ALLOW_REQUESTED_MODE"] = previous_allow

    def test_derived_interval_backfill_queues_source_interval(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)

        intraday = service.request_backfill("AAPL", "5m")
        monthly_request = service.request_backfill("AAPL", "1M")
        monthly_status = service.get_status("AAPL", "1M")

        self.assertEqual(store.created[0][1], "1m")
        self.assertEqual(store.created[1][1], "1D")
        self.assertEqual(intraday["status"], "queued")
        self.assertEqual(intraday["interval"], "5m")
        self.assertEqual(intraday["sourceInterval"], "1m")
        self.assertEqual(monthly_request["status"], "queued")
        self.assertEqual(monthly_request["interval"], "1M")
        self.assertEqual(monthly_request["sourceInterval"], "1D")
        self.assertEqual(monthly_status["status"], "queued")
        self.assertEqual(monthly_status["interval"], "1M")
        self.assertEqual(monthly_status["sourceInterval"], "1D")

    def test_force_backfill_flag_is_passed_to_status_store(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)

        requested = service.request_backfill("AAPL", "1D", force=True)

        self.assertEqual(requested["status"], "queued")
        self.assertTrue(store.created[0][5])

    def test_derived_interval_snapshot_metadata_uses_source_interval_status(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)

        service.request_backfill("AAPL", "1M")
        metadata = service.snapshot_metadata("AAPL", "1W", {
            "candles": [],
            "returnedCount": 0,
            "requestedLimit": 260,
            "storedCandleCount": 0,
            "targetStoredCount": 260,
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["backfillStatus"], "queued")
        self.assertFalse(metadata["canBackfill"])
        self.assertEqual(metadata["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["reasonCode"], "backfill_active")

    def test_succeeded_backfill_without_stored_coverage_is_not_ready(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "AAPL",
            "1m",
            start="2025-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
        )
        store.latest[("AAPL", "1m")] = {
            **record,
            "status": "succeeded",
            "finishedAt": "2026-06-25T00:01:00.000Z",
        }

        partial = service.snapshot_metadata("AAPL", "1m", {
            "candles": [{"timestamp": "2026-06-25T00:00:00.000Z"}],
            "returnedCount": 1,
            "requestedLimit": 390,
            "storedCandleCount": 1,
            "targetStoredCount": 98280,
            "availableFrom": "2026-06-25T00:00:00.000Z",
            "availableTo": "2026-06-25T00:00:00.000Z",
            "targetRangeFrom": "2025-06-25T00:00:00.000Z",
        })
        empty = service.snapshot_metadata("AAPL", "1m", {
            "candles": [],
            "returnedCount": 0,
            "requestedLimit": 390,
            "storedCandleCount": 0,
            "targetStoredCount": 98280,
            "targetRangeFrom": "2025-06-25T00:00:00.000Z",
        })

        self.assertEqual(partial["dataStatus"], "partial")
        self.assertEqual(partial["backfillStatus"], "succeeded")
        self.assertTrue(partial["canBackfill"])
        self.assertEqual(partial["coverage"]["reasonCode"], "insufficient_source_bars")
        self.assertFalse(partial["coverage"]["renderable"])
        self.assertEqual(empty["dataStatus"], "empty")
        self.assertTrue(empty["canBackfill"])
        self.assertEqual(empty["coverage"]["reasonCode"], "backfill_succeeded_without_complete_coverage")
        self.assertIn("Backfill completed", empty["message"])

    def test_unavailable_backfill_without_stored_coverage_can_retry(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "NVDA",
            "1D",
            start="2021-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
        )
        store.latest[("NVDA", "1D")] = {
            **record,
            "status": "unavailable",
            "error": "Alpaca credentials are not configured.",
            "finishedAt": "2026-06-25T00:01:00.000Z",
        }

        metadata = service.snapshot_metadata("NVDA", "1W", {
            "candles": [],
            "returnedCount": 0,
            "requestedLimit": 260,
            "storedCandleCount": 0,
            "targetStoredCount": 260,
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["backfillStatus"], "unavailable")
        self.assertTrue(metadata["canBackfill"])
        self.assertEqual(metadata["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["reasonCode"], "backfill_unavailable")

    def test_sparse_daily_coverage_is_not_renderable_ready_for_higher_timeframes(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "MU",
            "1D",
            start="2021-06-25T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
        )
        store.latest[("MU", "1D")] = {
            **record,
            "status": "succeeded",
            "finishedAt": "2026-06-25T00:01:00.000Z",
        }
        candles = [
            {"timestamp": "2021-06-29T00:00:00.000Z"},
            {"timestamp": "2022-01-27T00:00:00.000Z"},
            {"timestamp": "2023-01-27T00:00:00.000Z"},
            {"timestamp": "2024-01-27T00:00:00.000Z"},
            {"timestamp": "2025-01-27T00:00:00.000Z"},
            {"timestamp": "2026-01-27T00:00:00.000Z"},
        ]

        metadata = service.snapshot_metadata("MU", "1M", {
            "candles": candles,
            "returnedCount": len(candles),
            "requestedLimit": 120,
            "storedCandleCount": 8,
            "targetStoredCount": 1260,
            "availableFrom": "2021-06-29T00:00:00.000Z",
            "availableTo": "2026-01-27T00:00:00.000Z",
            "targetRangeFrom": "2021-06-25T00:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "partial")
        self.assertEqual(metadata["coverage"]["reasonCode"], "insufficient_source_bars")
        self.assertFalse(metadata["coverage"]["renderable"])
        self.assertEqual(metadata["coverage"]["sourceInterval"], "1D")

    def test_dense_completed_daily_backfill_allows_trading_calendar_tolerance(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "NVDA",
            "1D",
            start="2021-06-29T09:00:00.000Z",
            end="2026-06-28T09:00:00.000Z",
        )
        store.latest[("NVDA", "1D")] = {
            **record,
            "status": "succeeded",
            "finishedAt": "2026-06-28T09:01:00.000Z",
        }
        candles = [
            {"timestamp": f"2025-07-{day:02d}T00:00:00.000Z"}
            for day in range(1, 31)
        ]

        metadata = service.snapshot_metadata("NVDA", "1D", {
            "candles": candles,
            "returnedCount": 250,
            "requestedLimit": 250,
            "storedCandleCount": 1253,
            "targetStoredCount": 1260,
            "availableFrom": "2021-06-30T04:00:00.000Z",
            "availableTo": "2026-06-26T04:00:00.000Z",
            "targetRangeFrom": "2021-06-29T09:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["coverage"]["state"], "complete")
        self.assertEqual(metadata["coverage"]["reasonCode"], "coverage_complete")
        self.assertTrue(metadata["coverage"]["renderable"])

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

    def test_runtime_config_reports_safe_aws_s3_presence_only(self):
        with mock.patch.dict(os.environ, {
            "AWS_REGION": "ap-northeast-2",
            "AWS_ACCESS_KEY_ID": "AKIA_SHOULD_NOT_LEAK",
            "AWS_SECRET_ACCESS_KEY": "SECRET_SHOULD_NOT_LEAK",
            "AWS_SESSION_TOKEN": "",
            "S3_BUCKET": "gops-market-data-<aws-account-id>-ap-northeast-2-an",
            "S3_ENDPOINT_URL": "",
            "ALPACA_SECRET_NAME": "dev/alpaca",
            "APCA_API_KEY_ID": "",
            "APCA_API_SECRET_KEY": "",
        }, clear=False):
            payload = runtime_config()

        self.assertEqual(payload["s3"]["endpointMode"], "real-aws")
        self.assertEqual(payload["s3"]["endpoint"], "EMPTY")
        self.assertEqual(payload["s3"]["bucket"], "gops-market-data-<aws-account-id>-ap-northeast-2-an")
        self.assertEqual(payload["aws"]["accessKeyId"], "SET")
        self.assertEqual(payload["aws"]["secretAccessKey"], "SET")
        self.assertEqual(payload["aws"]["sessionToken"], "EMPTY")
        self.assertEqual(payload["alpaca"]["credentialSource"], "aws-secrets-manager")
        rendered = str(payload)
        self.assertNotIn("AKIA_SHOULD_NOT_LEAK", rendered)
        self.assertNotIn("SECRET_SHOULD_NOT_LEAK", rendered)


if __name__ == "__main__":
    unittest.main()
