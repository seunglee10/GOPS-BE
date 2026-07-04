import sys
import types
import unittest
import os
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(AGENT_SHARED), str(BACKEND)):
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

        def put(self, *args, **kwargs):
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
from app.market_data.monitor import routes as monitor_routes  # noqa: E402
from app.market_data.query import routes as query_routes  # noqa: E402
from app.contracts.chart import AgentChatMessage, AgentChatRequest  # noqa: E402
from app.routes import charts as chart_routes  # noqa: E402
from app.routes.health import runtime_config  # noqa: E402
from app.services import alfaka_market_data as market_data_service  # noqa: E402
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


class FakeNewsRedisProvider:
    def __init__(self, rows=None):
        self.rows = rows or []

    def localized_news_articles_for_symbols(self, symbols, limit=10, locale="ko-KR"):
        return self.rows[:limit]


class FakeNewsClickHouseProvider:
    def __init__(self, rows=None):
        self.rows = rows or []

    def localized_news_articles_for_symbols(self, symbols, limit=10, locale="ko-KR"):
        return self.rows[:limit]


class FakeNewsProvider:
    def __init__(self, redis_rows=None, clickhouse_rows=None):
        self.redis_provider = FakeNewsRedisProvider(redis_rows)
        self.clickhouse_provider = FakeNewsClickHouseProvider(clickhouse_rows)


class NoMutationRedis:
    def sadd(self, *args, **kwargs):
        raise AssertionError("GET ranking/hot routes must not mutate Redis subscription state")

    def hset(self, *args, **kwargs):
        raise AssertionError("GET ranking/hot routes must not mutate Redis subscription state")

    def delete(self, *args, **kwargs):
        raise AssertionError("GET ranking/hot routes must not mutate Redis subscription state")


class FakeHotRedisProvider:
    def __init__(self):
        self.redis = NoMutationRedis()

    def hot_symbols_snapshot(self):
        return None

    def recent_candles(self, symbol, interval, limit):
        raise AssertionError("per-symbol Redis scan should not run when ClickHouse hot ranking is available")


class FakeHotClickHouseProvider:
    def __init__(self):
        self.calls = []

    def hot_symbols_by_dollar_volume(self, symbols, limit=20):
        self.calls.append({"symbols": list(symbols), "limit": limit})
        return [
            {"symbol": "NVDA", "sessionDollarVolume": 3000, "lastPrice": 120, "changePercent": 2.5, "sourceUpdatedAt": "2026-06-25T15:30:00.000Z"},
            {"symbol": "AAPL", "sessionDollarVolume": 2000, "lastPrice": 190, "changePercent": 1.2, "sourceUpdatedAt": "2026-06-25T15:30:00.000Z"},
        ][:limit]

    def candles(self, symbol, interval, limit):
        if interval != "1D":
            return []
        previous_close = {"NVDA": 100, "AAPL": 200}.get(symbol, 100)
        return [
            {"timestamp": "2026-06-23T00:00:00.000Z", "close": previous_close - 5},
            {"timestamp": "2026-06-24T00:00:00.000Z", "close": previous_close},
        ]


class FakeHotProvider:
    def __init__(self):
        self.redis_provider = FakeHotRedisProvider()
        self.clickhouse_provider = FakeHotClickHouseProvider()

    def symbol_detail(self, symbol):
        names = {"NVDA": "NVIDIA Corporation", "AAPL": "Apple Inc."}
        return {"symbol": symbol, "name": names.get(symbol, symbol), "market": "NASDAQ"}


class FakeWatchlistRedis:
    def __init__(self):
        self.sets = {}
        self.hashes = {}
        self.values = {}
        self.expirations = {}

    def delete(self, key):
        self.sets.pop(key, None)
        self.hashes.pop(key, None)
        return 1

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def hset(self, key, mapping=None, **kwargs):
        values = mapping or kwargs
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def expire(self, key, ttl):
        self.expirations[key] = ttl
        return True


class FakeMonitorRedis:
    def __init__(self):
        self.sets = {}
        self.hashes = {}
        self.values = {}
        self.streams = {}
        self.deleted = []
        self.expirations = {}

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def hset(self, key, mapping=None, **kwargs):
        values = mapping or kwargs
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        self.expirations[key] = ttl
        return True

    def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.hashes.pop(key, None)
        self.values.pop(key, None)
        self.sets.pop(key, None)
        return 1

    def xadd(self, key, fields):
        self.streams.setdefault(key, []).append(fields)
        return f"{len(self.streams[key])}-0"

    def xlen(self, key):
        return len(self.streams.get(key, []))


class FakeWatchlistRedisProvider:
    def __init__(self):
        self.redis = FakeWatchlistRedis()

    def latest_price(self, symbol):
        return {"price": "191.5"} if symbol == "AAPL" else {}

    def recent_candles(self, symbol, interval, limit):
        prices = {"AAPL": 190, "AMZN": 240, "BRK.B": 410, "GOOGL": 354, "JPM": 200, "TSLA": 108}
        price = prices.get(symbol, 100)
        timestamp = "2026-06-29T15:30:00.000Z" if symbol in {"AMZN", "GOOGL"} else "2026-06-25T15:30:00.000Z"
        return [{
            "timestamp": timestamp,
            "open": price,
            "close": price + 1,
            "volume": 1000,
        }]


class FakeWatchlistClickHouseProvider:
    def candles(self, symbol, interval, limit):
        if interval == "1m" and symbol == "GOOGL":
            return [
                {"timestamp": "2026-06-26T19:59:00.000Z", "close": 350.0},
                {"timestamp": "2026-06-29T15:30:00.000Z", "close": 355.0},
            ]
        if interval != "1D":
            return []
        if symbol in {"AMZN", "GOOGL"}:
            return [
                {"timestamp": "2026-06-29T00:00:00.000Z", "close": 240.5 if symbol == "AMZN" else 354.5},
            ]
        closes = {
            "AAPL": (185, 190),
            "BRK.B": (395, 400),
            "JPM": (198, 199),
            "TSLA": (90, 100),
        }
        previous, latest = closes.get(symbol, (95, 100))
        return [
            {"timestamp": "2026-06-23T00:00:00.000Z", "close": previous},
            {"timestamp": "2026-06-24T00:00:00.000Z", "close": latest},
        ]


class FakeWatchlistProvider:
    def __init__(self):
        self.redis_provider = FakeWatchlistRedisProvider()
        self.clickhouse_provider = FakeWatchlistClickHouseProvider()

    def search_symbols(self, query, limit=20):
        records = [
            {"symbol": "AAPL", "name": "Apple Inc.", "market": "NASDAQ"},
            {"symbol": "MSFT", "name": "Microsoft Corporation", "market": "NASDAQ"},
            {"symbol": "BRK.B", "name": "Berkshire Hathaway Inc. Class B", "market": "NYSE"},
            {"symbol": "AMD", "name": "Advanced Micro Devices, Inc.", "market": "NASDAQ"},
        ]
        normalized = query.upper()
        return [
            record for record in records
            if normalized in f"{record['symbol']} {record['name']}".upper()
        ][:limit]

    def symbol_detail(self, symbol):
        names = {
            "AAPL": "Apple Inc.",
            "BRK.B": "Berkshire Hathaway Inc. Class B",
            "JPM": "JPMorgan Chase & Co.",
        }
        return {"symbol": symbol, "name": names.get(symbol, symbol), "market": "NASDAQ"}


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

    def queue_metrics(self):
        return {
            "queueBackend": "streams",
            "observedAt": "2026-06-25T13:30:00.000Z",
            "stream": {
                "retainedLength": 2,
                "pendingCount": 1,
                "undeliveredCount": 1,
                "backlogCount": 2,
            },
            "deadLetter": {"length": 0},
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

    def latest_news(self, symbol, limit=10, locale="ko-KR"):
        return self.service.latest_news(symbol, limit=limit, locale=locale)

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default", force=False):
        return self.service.request_backfill(symbol, interval, start=start, end=end, mode=mode, force=force)

    def backfill_status(self, symbol, interval, request_id=None):
        return self.service.backfill_status(symbol, interval, request_id=request_id)

    def backfill_queue_metrics(self):
        return self.service.backfill_queue_metrics()


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

    def test_latest_news_uses_redis_cache_and_normalizes_response_shape(self):
        service = MarketDataQueryService(FakeNewsProvider(redis_rows=[{
            "targetSymbol": "NVDA",
            "symbols": ["NVDA", "AMD"],
            "localizedHeadline": "엔비디아 최신 뉴스",
            "localizedSummary": "데이터센터 수요가 강합니다.",
            "url": "https://example.com/nvda",
            "source": "alpaca",
            "publishedAt": "2026-07-01T12:00:00.000Z",
            "impactDirection": "positive",
        }]), backfill_service=FakeBackfillService())

        payload = service.latest_news("nvda", limit=5)

        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["source"], "redis")
        self.assertEqual(payload["items"][0]["title"], "엔비디아 최신 뉴스")
        self.assertEqual(payload["items"][0]["summary"], "데이터센터 수요가 강합니다.")
        self.assertEqual(payload["items"][0]["impactDirection"], "positive")

    def test_latest_news_falls_back_to_clickhouse_when_redis_empty(self):
        service = MarketDataQueryService(FakeNewsProvider(clickhouse_rows=[{
            "target_symbol": "AAPL",
            "headline": "Apple headline",
            "summary": "Apple summary",
            "published_at": "2026-07-01T12:00:00.000Z",
        }]), backfill_service=FakeBackfillService())

        payload = service.latest_news("aapl", limit=5)

        self.assertEqual(payload["source"], "clickhouse")
        self.assertEqual(payload["items"][0]["symbol"], "AAPL")
        self.assertEqual(payload["items"][0]["title"], "Apple headline")

    def test_latest_news_route_delegates_to_query_service(self):
        previous = query_routes.get_query_service
        query_routes.get_query_service = lambda: FakeQueryService(FakeNewsProvider(redis_rows=[{
            "targetSymbol": "NVDA",
            "headline": "NVIDIA",
            "summary": "News summary",
        }]))
        try:
            payload = query_routes.market_latest_news("nvda", limit=3)
        finally:
            query_routes.get_query_service = previous

        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["items"][0]["title"], "NVIDIA")

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
        os.environ["ALPACA_SYMBOLS"] = "IBM,ORCL"
        try:
            self.assertEqual(configured_symbols(), ["IBM", "ORCL"])
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
            queue = chart_routes.chart_backfill_queue()
        finally:
            chart_routes.get_query_service = previous

        self.assertEqual(requested["requestId"], "backfill:INTC:1m:test")
        self.assertEqual(status["status"], "queued")
        self.assertEqual(queue["queueBackend"], "streams")
        self.assertEqual(queue["stream"]["backlogCount"], 2)

    def test_monitor_subscription_route_writes_manual_source_for_controller(self):
        fake_redis = FakeMonitorRedis()
        previous = monitor_routes.get_monitor_service
        monitor_routes.get_monitor_service = lambda: __import__(
            "app.market_data.monitor.service",
            fromlist=["MarketDataMonitorService"],
        ).MarketDataMonitorService(redis_client=fake_redis)
        try:
            payload = monitor_routes.add_market_data_subscription(monitor_routes.SubscriptionRequestBody(
                symbol="aapl",
                layers=["trades", "quotes", "candles"],
                reason="test",
                ttlSeconds=600,
            ))
            subscriptions = monitor_routes.market_data_monitor_subscriptions()
        finally:
            monitor_routes.get_monitor_service = previous

        from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService

        self.assertEqual(fake_redis.sets["gops:market:on-demand:v1:subscription:source:manual:symbols"], {"AAPL"})
        self.assertEqual(fake_redis.sets["gops:market:on-demand:v1:subscription:source:manual:AAPL"], {"candles,quotes,trades"})
        self.assertNotIn("gops:market:on-demand:v1:subscription:symbols", fake_redis.sets)
        self.assertTrue(payload["pendingReconcile"])

        RealtimeSubscriptionCohortService(fake_redis).reconcile()
        monitor_routes.get_monitor_service = lambda: __import__(
            "app.market_data.monitor.service",
            fromlist=["MarketDataMonitorService"],
        ).MarketDataMonitorService(redis_client=fake_redis)
        try:
            subscriptions = monitor_routes.market_data_monitor_subscriptions()
        finally:
            monitor_routes.get_monitor_service = previous
        self.assertEqual(fake_redis.sets["gops:market:on-demand:v1:subscription:symbols"], {"AAPL"})
        self.assertEqual(fake_redis.hashes["gops:market:on-demand:v1:subscription:symbol:AAPL"]["layers"], "candles,quotes,trades")
        self.assertEqual(payload["subscription"]["symbol"], "AAPL")
        self.assertEqual(subscriptions["symbols"][0]["layers"], ["candles", "quotes", "trades"])

    def test_monitor_subscription_rejects_quotes_without_trades(self):
        fake_redis = FakeMonitorRedis()
        previous = monitor_routes.get_monitor_service
        monitor_routes.get_monitor_service = lambda: __import__(
            "app.market_data.monitor.service",
            fromlist=["MarketDataMonitorService"],
        ).MarketDataMonitorService(redis_client=fake_redis)
        try:
            with self.assertRaises(HTTPException) as raised:
                monitor_routes.add_market_data_subscription(monitor_routes.SubscriptionRequestBody(
                    symbol="msft",
                    layers=["quotes"],
                    reason="test",
                    ttlSeconds=600,
                ))
        finally:
            monitor_routes.get_monitor_service = previous

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("quotes layer requires", str(raised.exception.detail))

    def test_chart_symbols_route_uses_provider_search_when_no_default_universe_exists(self):
        provider = FakeWatchlistProvider()
        previous_provider = market_data_service.get_market_data_provider
        market_data_service.get_market_data_provider = lambda: provider
        try:
            brk = chart_routes.chart_symbols(query="brk", limit=10)
            adbe = chart_routes.chart_symbols(query="adbe", limit=10)
        finally:
            market_data_service.get_market_data_provider = previous_provider

        self.assertEqual([item["symbol"] for item in brk["symbols"]], ["BRK.B"])
        self.assertEqual([item["symbol"] for item in adbe["symbols"]], ["ADBE"])

    def test_watchlist_change_percent_uses_previous_close_not_intraday_open(self):
        provider = FakeWatchlistProvider()
        previous = market_data_service.get_market_data_provider
        market_data_service.get_market_data_provider = lambda: provider
        try:
            payload = market_data_service.symbol_summaries_for(["TSLA"])
        finally:
            market_data_service.get_market_data_provider = previous

        self.assertEqual(payload[0]["lastPrice"], 100.0)
        self.assertEqual(payload[0]["priceSource"], "clickhouse")
        self.assertEqual(payload[0]["changePercent"], 11.11)

    def test_watchlist_change_percent_does_not_fake_from_intraday_open_without_previous_close(self):
        provider = FakeWatchlistProvider()
        previous = market_data_service.get_market_data_provider
        market_data_service.get_market_data_provider = lambda: provider
        try:
            payload = market_data_service.symbol_summaries_for(["AMZN"])
        finally:
            market_data_service.get_market_data_provider = previous

        self.assertEqual(payload[0]["lastPrice"], 240.5)
        self.assertEqual(payload[0]["priceSource"], "clickhouse")
        self.assertIsNone(payload[0]["changePercent"])

    def test_watchlist_change_percent_stays_empty_when_daily_baseline_missing(self):
        provider = FakeWatchlistProvider()
        previous = market_data_service.get_market_data_provider
        market_data_service.get_market_data_provider = lambda: provider
        try:
            payload = market_data_service.symbol_summaries_for(["GOOGL"])
        finally:
            market_data_service.get_market_data_provider = previous

        self.assertEqual(payload[0]["lastPrice"], 354.5)
        self.assertEqual(payload[0]["priceSource"], "clickhouse")
        self.assertIsNone(payload[0]["changePercent"])

    def test_watchlist_replace_uses_user_key_until_controller_reconciles(self):
        provider = FakeWatchlistProvider()
        previous = market_data_service.get_market_data_provider
        previous_universe = market_data_service.configured_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.configured_universe_symbols = lambda: ["AAPL", "MSFT", "NVDA"]
        try:
            payload = market_data_service.replace_watchlist_symbols("user-a", ["aapl", "msft"])
            redis_state = provider.redis_provider.redis
        finally:
            market_data_service.get_market_data_provider = previous
            market_data_service.configured_universe_symbols = previous_universe

        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["AAPL", "MSFT"])
        self.assertNotIn("gops:market:on-demand:v1:ui:watchlist:symbols", redis_state.sets)
        self.assertEqual(redis_state.sets["gops:market:on-demand:v1:user:user-a:watchlist:symbols"], {"AAPL", "MSFT"})
        self.assertEqual(redis_state.sets["gops:market:on-demand:v1:subscription:users:watchlist"], {"user-a"})
        self.assertNotIn("gops:market:on-demand:v1:subscription:symbols", redis_state.sets)

        from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService

        RealtimeSubscriptionCohortService(redis_state).reconcile()
        self.assertEqual(redis_state.sets["gops:market:on-demand:v1:subscription:source:watchlist:AAPL"], {"user-a"})
        self.assertEqual(redis_state.sets["gops:market:on-demand:v1:subscription:symbols"], {"AAPL", "MSFT"})
        self.assertEqual(redis_state.hashes["gops:market:on-demand:v1:subscription:symbol:AAPL"]["layers"], "quotes,trades")
        self.assertEqual(redis_state.hashes["gops:market:on-demand:v1:subscription:symbol:AAPL"]["sources"], "watchlist")
        self.assertEqual(redis_state.hashes["gops:market:on-demand:v1:subscription:symbol:AAPL"]["source"], "subscription-controller")

    def test_watchlist_remove_preserves_active_chart_subscription_source(self):
        provider = FakeWatchlistProvider()
        redis_state = provider.redis_provider.redis
        from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
        controller = RealtimeSubscriptionCohortService(redis_state)
        controller.replace_user_watchlist("user-a", ["AAPL"])
        controller.refresh_active_chart("user-a", "session-1", "AAPL", 60)
        previous = market_data_service.get_market_data_provider
        previous_universe = market_data_service.configured_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.configured_universe_symbols = lambda: ["AAPL"]
        try:
            market_data_service.replace_watchlist_symbols("user-a", [])
        finally:
            market_data_service.get_market_data_provider = previous
            market_data_service.configured_universe_symbols = previous_universe

        controller.reconcile()
        self.assertEqual(redis_state.sets["gops:market:on-demand:v1:subscription:symbols"], {"AAPL"})
        self.assertEqual(redis_state.hashes["gops:market:on-demand:v1:subscription:symbol:AAPL"]["sources"], "active-chart")
        self.assertEqual(redis_state.hashes["gops:market:on-demand:v1:subscription:symbol:AAPL"]["reason"], "active-chart-session")

    def test_sp500_symbol_page_uses_registry_without_subscribing_every_symbol(self):
        provider = FakeWatchlistProvider()
        previous_provider = market_data_service.get_market_data_provider
        previous_sp500 = market_data_service.sp500_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.sp500_universe_symbols = lambda: ["AAPL", "MSFT", "NVDA"]
        try:
            payload = market_data_service.market_symbol_page("", page=1, page_size=2)
            redis_state = provider.redis_provider.redis
        finally:
            market_data_service.get_market_data_provider = previous_provider
            market_data_service.sp500_universe_symbols = previous_sp500

        self.assertEqual(payload["total"], 3)
        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["AAPL", "MSFT"])
        self.assertEqual(payload["symbols"][0]["priceSource"], "live")
        self.assertEqual(payload["symbols"][1]["priceSource"], "clickhouse")
        self.assertIn("gops:market:on-demand:v1:latest:closed:candle:MSFT:1D", redis_state.values)
        self.assertNotIn("gops:market:on-demand:v1:subscription:symbols", redis_state.sets)

    def test_sp500_symbol_page_requests_latest_daily_backfill_when_price_missing(self):
        class NoPriceRedisProvider:
            def __init__(self):
                self.redis = FakeWatchlistRedis()

            def latest_price(self, symbol):
                return {}

        class NoPriceClickHouseProvider:
            def candles(self, symbol, interval, limit):
                return []

        class NoPriceProvider:
            def __init__(self):
                self.redis_provider = NoPriceRedisProvider()
                self.clickhouse_provider = NoPriceClickHouseProvider()

            def symbol_detail(self, symbol):
                return {"symbol": symbol, "name": symbol, "market": "US"}

        class RecordingLatestPriceBackfill:
            def __init__(self):
                self.calls = []

            def request_backfill(self, symbol, interval, start=None, end=None, mode="default", force=False):
                self.calls.append((symbol, interval, start, end, mode, force))
                return {
                    "symbol": symbol,
                    "interval": interval,
                    "sourceInterval": interval,
                    "requestId": f"backfill:{symbol}:{interval}:latest",
                    "status": "queued",
                    "deduplicated": False,
                }

        provider = NoPriceProvider()
        backfill = RecordingLatestPriceBackfill()
        previous_provider = market_data_service.get_market_data_provider
        previous_sp500 = market_data_service.sp500_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.sp500_universe_symbols = lambda: ["MSFT"]
        try:
            payload = market_data_service.market_symbol_page("", page=1, page_size=1, backfill_service=backfill)
        finally:
            market_data_service.get_market_data_provider = previous_provider
            market_data_service.sp500_universe_symbols = previous_sp500

        self.assertEqual(len(backfill.calls), 1)
        symbol, interval, start, end, mode, force = backfill.calls[0]
        self.assertEqual((symbol, interval, mode, force), ("MSFT", "1D", "default", False))
        self.assertIsInstance(start, str)
        self.assertIsInstance(end, str)
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        self.assertLessEqual(end_dt - start_dt, timedelta(days=31))
        self.assertEqual(payload["symbols"][0]["lastPrice"], None)
        self.assertEqual(payload["symbols"][0]["priceSource"], "latest-backfill")
        self.assertEqual(payload["symbols"][0]["priceStatus"], "loading")
        self.assertEqual(payload["symbols"][0]["latestPriceBackfill"]["status"], "queued")

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

    def test_oversized_backfill_request_returns_bad_request(self):
        class RejectingBackfillStore:
            def create_request(self, *args, **kwargs):
                raise ValueError("Rejected oversized 1m gapfill")

        service = BackfillService(store=RejectingBackfillStore())

        with self.assertRaises(HTTPException) as raised:
            service.request_backfill(
                "AAPL",
                "1m",
                start="2020-07-01T00:00:00.000Z",
                end="2026-06-30T00:00:00.000Z",
                force=True,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Rejected oversized", str(raised.exception.detail))

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
            "targetStoredCount": 1512,
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["backfillStatus"], "queued")
        self.assertEqual(metadata["repairStatus"], "gapfill_active")
        self.assertFalse(metadata["canBackfill"])
        self.assertEqual(metadata["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["reasonCode"], "backfill_active")
        self.assertEqual(metadata["coverage"]["repairStatus"], "gapfill_active")

    def test_succeeded_backfill_without_stored_coverage_is_not_ready(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "AAPL",
            "1m",
            start="2023-06-26T00:00:00.000Z",
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
            "targetStoredCount": 5460,
            "availableFrom": "2026-06-25T00:00:00.000Z",
            "availableTo": "2026-06-25T00:00:00.000Z",
            "targetRangeFrom": "2026-06-11T00:00:00.000Z",
        })
        empty = service.snapshot_metadata("AAPL", "1m", {
            "candles": [],
            "returnedCount": 0,
            "requestedLimit": 390,
            "storedCandleCount": 0,
            "targetStoredCount": 5460,
            "targetRangeFrom": "2026-06-11T00:00:00.000Z",
        })

        self.assertEqual(partial["dataStatus"], "partial")
        self.assertEqual(partial["backfillStatus"], "succeeded")
        self.assertEqual(partial["repairStatus"], "gapfill_required")
        self.assertTrue(partial["canBackfill"])
        self.assertEqual(partial["coverage"]["reasonCode"], "insufficient_source_bars")
        self.assertFalse(partial["coverage"]["renderable"])
        self.assertEqual(empty["dataStatus"], "empty")
        self.assertEqual(empty["repairStatus"], "gapfill_required")
        self.assertTrue(empty["canBackfill"])
        self.assertEqual(empty["coverage"]["reasonCode"], "backfill_succeeded_without_complete_coverage")
        self.assertIn("Backfill completed", empty["message"])

    def test_unavailable_backfill_without_stored_coverage_can_retry(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "NVDA",
            "1D",
            start="2023-06-26T00:00:00.000Z",
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
            "targetStoredCount": 1512,
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["backfillStatus"], "unavailable")
        self.assertEqual(metadata["repairStatus"], "gapfill_failed")
        self.assertTrue(metadata["canBackfill"])
        self.assertEqual(metadata["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["reasonCode"], "backfill_unavailable")

    def test_sparse_daily_coverage_is_not_renderable_ready_for_higher_timeframes(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "MU",
            "1D",
            start="2023-06-26T00:00:00.000Z",
            end="2026-06-25T00:00:00.000Z",
        )
        store.latest[("MU", "1D")] = {
            **record,
            "status": "succeeded",
            "finishedAt": "2026-06-25T00:01:00.000Z",
        }
        candles = [
            {"timestamp": "2023-07-27T00:00:00.000Z"},
            {"timestamp": "2024-01-27T00:00:00.000Z"},
            {"timestamp": "2025-01-27T00:00:00.000Z"},
            {"timestamp": "2026-01-27T00:00:00.000Z"},
        ]

        metadata = service.snapshot_metadata("MU", "1M", {
            "candles": candles,
            "returnedCount": len(candles),
            "requestedLimit": 120,
            "storedCandleCount": 8,
            "targetStoredCount": 1512,
            "availableFrom": "2023-07-27T00:00:00.000Z",
            "availableTo": "2026-01-27T00:00:00.000Z",
            "targetRangeFrom": "2020-06-26T00:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "partial")
        self.assertEqual(metadata["repairStatus"], "gapfill_required")
        self.assertEqual(metadata["coverage"]["reasonCode"], "insufficient_source_bars")
        self.assertFalse(metadata["coverage"]["renderable"])
        self.assertEqual(metadata["coverage"]["sourceInterval"], "1D")

    def test_renderable_visible_range_can_be_ready_while_history_preload_is_required(self):
        service = BackfillService(store=RecordingBackfillStore())
        candles = [
            {"timestamp": f"2026-06-25T1{index // 60}:{index % 60:02d}:00.000Z"}
            for index in range(30)
        ]

        metadata = service.snapshot_metadata("NVDA", "1m", {
            "candles": candles,
            "returnedCount": 30,
            "requestedLimit": 30,
            "storedCandleCount": 30,
            "targetStoredCount": 5460,
            "availableFrom": "2026-06-25T10:00:00.000Z",
            "availableTo": "2026-06-25T10:29:00.000Z",
            "targetRangeFrom": "2026-06-11T00:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "history_preload_required")
        self.assertTrue(metadata["canBackfill"])
        self.assertEqual(metadata["coverage"]["state"], "partial")
        self.assertTrue(metadata["coverage"]["renderable"])

    def test_intraday_renderability_allows_weekend_and_overnight_gaps(self):
        service = BackfillService(store=RecordingBackfillStore())
        friday_start = datetime.fromisoformat("2026-06-26T19:00:00+00:00")
        monday_start = datetime.fromisoformat("2026-06-29T13:30:00+00:00")
        candles = [
            {"timestamp": (friday_start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(60)
        ] + [
            {"timestamp": (monday_start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(60)
        ]

        metadata = service.snapshot_metadata("NVDA", "1m", {
            "candles": candles,
            "returnedCount": 120,
            "requestedLimit": 120,
            "storedCandleCount": 120,
            "targetStoredCount": 5460,
            "availableFrom": candles[0]["timestamp"],
            "availableTo": candles[-1]["timestamp"],
            "targetRangeFrom": "2026-06-11T00:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "history_preload_required")
        self.assertTrue(metadata["coverage"]["renderable"])

    def test_intraday_renderability_rejects_same_session_sparse_gap(self):
        service = BackfillService(store=RecordingBackfillStore())
        early_start = datetime.fromisoformat("2026-06-25T13:30:00+00:00")
        late_start = datetime.fromisoformat("2026-06-25T18:00:00+00:00")
        candles = [
            {"timestamp": (early_start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(10)
        ] + [
            {"timestamp": (late_start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(10)
        ]

        metadata = service.snapshot_metadata("NVDA", "1m", {
            "candles": candles,
            "returnedCount": 20,
            "requestedLimit": 20,
            "storedCandleCount": 30,
            "targetStoredCount": 5460,
            "availableFrom": candles[0]["timestamp"],
            "availableTo": candles[-1]["timestamp"],
            "targetRangeFrom": "2026-06-11T00:00:00.000Z",
        })

        self.assertFalse(metadata["coverage"]["renderable"])
        self.assertEqual(metadata["coverage"]["renderabilityReasonCode"], "returned_window_sparse")
        self.assertEqual(metadata["coverage"]["gapRanges"], [{
            "start": "2026-06-25T13:40:00.000Z",
            "end": "2026-06-25T18:00:00.000Z",
            "missingCount": 260,
        }])

    def test_intraday_renderability_allows_after_hours_sparse_bars(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "AAPL",
            "1m",
            start="2026-06-16T00:00:00.000Z",
            end="2026-06-30T00:00:00.000Z",
        )
        store.latest[("AAPL", "1m")] = {
            **record,
            "status": "succeeded",
            "finishedAt": "2026-06-30T00:01:00.000Z",
        }
        first_after_hours = datetime.fromisoformat("2026-06-29T21:10:00+00:00")
        candles = [
            {"timestamp": (first_after_hours + timedelta(minutes=index * 3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(40)
        ]

        metadata = service.snapshot_metadata("AAPL", "1m", {
            "candles": candles,
            "returnedCount": len(candles),
            "requestedLimit": len(candles),
            "storedCandleCount": 5460,
            "targetStoredCount": 5460,
            "availableFrom": "2026-06-16T00:00:00.000Z",
            "availableTo": candles[-1]["timestamp"],
            "targetRangeFrom": "2026-06-16T00:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "none")
        self.assertTrue(metadata["coverage"]["renderable"])
        self.assertIsNone(metadata["coverage"]["renderabilityReasonCode"])
        self.assertEqual(metadata["coverage"]["gapRanges"], [])

    def test_dense_completed_daily_backfill_allows_trading_calendar_tolerance(self):
        store = RecordingBackfillStore()
        service = BackfillService(store=store)
        record, _ = store.create_request(
            "NVDA",
            "1D",
            start="2020-06-29T09:00:00.000Z",
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
            "storedCandleCount": 1500,
            "targetStoredCount": 1512,
            "availableFrom": "2020-06-30T04:00:00.000Z",
            "availableTo": "2026-06-26T04:00:00.000Z",
            "targetRangeFrom": "2020-06-29T09:00:00.000Z",
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "none")
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

    @unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient dependency is not installed")
    def test_chart_mutation_routes_require_authenticated_user_when_auth_enabled(self):
        from app.main import create_app

        with mock.patch.dict(os.environ, {
            "AUTH_ENABLED": "true",
            "AUTH_SESSION_SECRET": "test-session-secret",
            "AUTH_REDIS_URL": "",
            "AUTH_REDIS_KEY_PREFIX": "gops:test-auth",
        }, clear=False):
            client = TestClient(create_app())
            watchlist = client.put("/api/charts/watchlist", json={"symbols": ["NVDA"]})
            portfolio = client.put("/api/charts/subscription-cohorts/portfolio", json={"symbols": ["AAPL"]})
            backfill = client.post("/api/charts/backfill", json={"symbol": "NVDA", "interval": "1m"})

        self.assertEqual(watchlist.status_code, 401)
        self.assertEqual(portfolio.status_code, 401)
        self.assertEqual(backfill.status_code, 401)

    def test_monitor_overview_documents_quote_and_raw_s3_policy(self):
        fake_redis = FakeMonitorRedis()
        previous = monitor_routes.get_monitor_service
        monitor_routes.get_monitor_service = lambda: __import__(
            "app.market_data.monitor.service",
            fromlist=["MarketDataMonitorService"],
        ).MarketDataMonitorService(redis_client=fake_redis)
        try:
            payload = monitor_routes.market_data_monitor_overview()
        finally:
            monitor_routes.get_monitor_service = previous

        self.assertEqual(payload["quotesPersistence"], "redis-websocket-s3-clickhouse")
        self.assertEqual(payload["rawS3Role"], "backup-only")
        self.assertEqual(payload["redisCandleCacheLimit"], 120)

    def test_hot_symbol_summaries_use_clickhouse_ranking_before_symbol_scan(self):
        provider = FakeHotProvider()
        previous_provider = market_data_service.get_market_data_provider
        previous_universe = market_data_service.configured_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.configured_universe_symbols = lambda: ["NVDA", "AAPL", "MSFT"]
        try:
            payload = market_data_service.hot_symbol_summaries(limit=2)
        finally:
            market_data_service.get_market_data_provider = previous_provider
            market_data_service.configured_universe_symbols = previous_universe

        self.assertEqual(payload["ranking"]["limit"], 2)
        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["NVDA", "AAPL"])
        self.assertEqual(payload["symbols"][0]["name"], "NVIDIA Corporation")
        self.assertEqual(payload["symbols"][0]["changePercent"], 20.0)
        self.assertEqual(provider.clickhouse_provider.calls, [{"symbols": ["NVDA", "AAPL", "MSFT"], "limit": 2}])

    def test_runtime_config_reports_safe_aws_s3_presence_only(self):
        with mock.patch.dict(os.environ, {
            "AWS_REGION": "ap-northeast-2",
            "AWS_ACCESS_KEY_ID": "AKIA_SHOULD_NOT_LEAK",
            "AWS_SECRET_ACCESS_KEY": "SECRET_SHOULD_NOT_LEAK",
            "AWS_SESSION_TOKEN": "",
            "S3_BUCKET": "gops-market-data-<aws-account-id>-ap-northeast-2-an",
            "S3_ENDPOINT_URL": "",
            "S3_RAW_PREFIX": "",
            "S3_FINAL_PREFIX": "",
            "S3_LIVE_PREFIX": "",
            "S3_MANIFEST_PREFIX": "",
            "S3_PROCESSED_FORMAT": "parquet",
            "HISTORICAL_ADJUSTMENT": "split",
            "ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT": "false",
            "CLICKHOUSE_REQUIRE_CANONICAL_CANDLES": "true",
            "S3_REQUIRE_CANONICAL_PROCESSED_CANDLES": "true",
            "ALFAKA_REQUEST_CONFIG": "systems/market-data/config/market-data-request.json",
            "ALPACA_UNIVERSE": "",
            "ALPACA_CHANNELS": "bars,updatedBars,dailyBars,statuses",
            "ALPACA_FEED_PROFILES": "sip,boats",
            "BACKFILL_INITIAL_LOAD_1M_MIN_START": "2020-07-01T00:00:00Z",
            "ALPACA_CREDENTIAL_SOURCE": "aws-secrets-manager",
            "ALPACA_SECRET_NAME": "dev/alpaca",
            "APCA_API_KEY_ID": "",
            "APCA_API_SECRET_KEY": "",
        }, clear=False):
            payload = runtime_config()

        self.assertEqual(payload["s3"]["endpointMode"], "real-aws")
        self.assertEqual(payload["s3"]["endpoint"], "EMPTY")
        self.assertEqual(payload["s3"]["bucket"], "gops-market-data-<aws-account-id>-ap-northeast-2-an")
        self.assertEqual(payload["s3"]["finalPrefix"], "")
        self.assertEqual(payload["s3"]["manifestPrefix"], "")
        self.assertEqual(payload["aws"]["accessKeyId"], "SET")
        self.assertEqual(payload["aws"]["secretAccessKey"], "SET")
        self.assertEqual(payload["aws"]["sessionToken"], "EMPTY")
        self.assertEqual(payload["alpaca"]["configuredCredentialSource"], "aws-secrets-manager")
        self.assertEqual(payload["alpaca"]["credentialSource"], "aws-secrets-manager")
        self.assertEqual(payload["canonical"]["historicalAdjustment"], "split")
        self.assertFalse(payload["canonical"]["allowNonCanonicalHistoricalAdjustment"])
        self.assertTrue(payload["canonical"]["clickhouseRequireCanonicalCandles"])
        self.assertTrue(payload["canonical"]["s3RequireCanonicalProcessedCandles"])
        self.assertEqual(payload["canonical"]["s3ProcessedFormat"], "parquet")
        self.assertEqual(payload["warnings"], [])
        rendered = str(payload)
        self.assertNotIn("AKIA_SHOULD_NOT_LEAK", rendered)
        self.assertNotIn("SECRET_SHOULD_NOT_LEAK", rendered)

    def test_runtime_config_reports_redacted_stale_env_warnings(self):
        with mock.patch.dict(os.environ, {
            "ALFAKA_REQUEST_CONFIG": "config/market-data-request.json",
            "ALPACA_UNIVERSE": "semiconductor-100",
            "ALPACA_CHANNELS": "bars,updatedBars,trades",
            "S3_PROCESSED_FORMAT": "jsonl",
            "HISTORICAL_ADJUSTMENT": "raw",
            "ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT": "true",
            "CLICKHOUSE_REQUIRE_CANONICAL_CANDLES": "false",
            "S3_REQUIRE_CANONICAL_PROCESSED_CANDLES": "false",
            "BACKFILL_INITIAL_LOAD_1M_MIN_START": "2025-04-01T00:00:00Z",
            "ALPACA_CREDENTIAL_SOURCE": "bogus",
            "ALPACA_COLLECTION_SYMBOL_SOURCE": "on-demand",
        }, clear=False):
            payload = runtime_config()

        self.assertEqual(payload["alpaca"]["configuredCredentialSource"], "invalid")
        self.assertEqual(payload["canonical"]["historicalAdjustment"], "raw")
        self.assertTrue(payload["canonical"]["allowNonCanonicalHistoricalAdjustment"])
        self.assertFalse(payload["canonical"]["clickhouseRequireCanonicalCandles"])
        self.assertFalse(payload["canonical"]["s3RequireCanonicalProcessedCandles"])
        self.assertIn("stale_request_config_path", payload["warnings"])
        self.assertIn("alpaca_universe_without_registry_source", payload["warnings"])
        self.assertIn("alpaca_channels_missing_dailyBars", payload["warnings"])
        self.assertIn("alpaca_channels_missing_statuses", payload["warnings"])
        self.assertIn("s3_processed_format_not_parquet", payload["warnings"])
        self.assertIn("historical_adjustment_not_split", payload["warnings"])
        self.assertIn("noncanonical_historical_adjustment_allowed", payload["warnings"])
        self.assertIn("clickhouse_canonical_filter_disabled", payload["warnings"])
        self.assertIn("s3_canonical_manifest_filter_disabled", payload["warnings"])
        self.assertIn("1m_lazy_floor_not_6y", payload["warnings"])
        self.assertIn("invalid_alpaca_credential_source", payload["warnings"])
        self.assertNotIn("semiconductor-100", str(payload))


if __name__ == "__main__":
    unittest.main()
