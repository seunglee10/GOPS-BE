import sys
import types
import unittest
import os
import json
from datetime import datetime, timedelta, timezone
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

    def Depends(value=None, **kwargs):
        return value

    sys.modules["fastapi"] = types.SimpleNamespace(
        APIRouter=APIRouter,
        Depends=Depends,
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

from app.market_data.query import service as query_service_module  # noqa: E402
from app.market_data.query.service import MarketDataQueryService  # noqa: E402
from app.market_data.calendar.service import next_market_open_payload, us_equity_holidays  # noqa: E402
from app.market_data.backfill.service import BackfillService  # noqa: E402
from app.market_data.compare.service import ChartCompareService  # noqa: E402
from app.market_data.fill.service import OnDemandFillService  # noqa: E402
from app.market_data.fundamentals.service import FundamentalsAdapter, FundamentalsRecord, StoreFundamentalsAdapter, records_from_payload  # noqa: E402
from app.market_data.heatmap import service as heatmap_service  # noqa: E402
from app.market_data.indices import service as indices_service  # noqa: E402
from app.market_data.monitor import routes as monitor_routes  # noqa: E402
from app.market_data.query import routes as query_routes  # noqa: E402
from app.contracts.chart import AgentChatMessage, AgentChatRequest  # noqa: E402
from app.auth.models import AuthenticatedUser  # noqa: E402
from app.routes import charts as chart_routes  # noqa: E402
from app.routes.health import runtime_config  # noqa: E402
from app.services import alfaka_market_data as market_data_service  # noqa: E402
from app.services.alfaka_market_data import configured_symbols  # noqa: E402
from app.services.ai_agents import build_agent_market_analysis_context, chart_context_for_agent_prompt, is_live_feed_status_request, openai_agent_chat  # noqa: E402
from alfaka.common.redis_keys import RedisKeyBuilder  # noqa: E402


class FakeProvider:
    def __init__(self, fail_snapshot=False):
        self.fail_snapshot = fail_snapshot
        self.last_limit = None

    def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None, ma_windows=None):
        self.last_limit = limit
        self.last_ma_windows = ma_windows
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


class FakeCompareRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    def expire(self, key, ttl):
        self.ttls[key] = ttl


class FakeCompareProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.redis_provider = types.SimpleNamespace(redis=FakeCompareRedis())

    def symbol_detail(self, symbol):
        return {"symbol": symbol, "name": f"{symbol} Corporation", "exchange": "NASDAQ"}


class FakeIndicatorRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl


class FakeIndicatorRedisProvider:
    def __init__(self):
        self.redis = FakeIndicatorRedis()


class FakeIndicatorProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.redis_provider = FakeIndicatorRedisProvider()
        self.calls = []

    def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None, ma_windows=None):
        self.calls.append({
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "before": before,
            "fromTime": from_time,
            "toTime": to_time,
            "maWindows": ma_windows,
        })
        start = datetime(2026, 6, 25, 13, 0, tzinfo=timezone.utc)
        parsed_from = datetime.fromisoformat(from_time.replace("Z", "+00:00")) if from_time else start
        candles = []
        for index in range(limit):
            timestamp = parsed_from + timedelta(minutes=index)
            close = index + 1
            candles.append({
                "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "open": close - 0.2,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 100 + index,
            })
        return {
            "symbol": symbol,
            "interval": interval,
            "source": "unit",
            "feed": "test",
            "snapshotCursor": "indicator-cursor",
            "dataStatus": "ready",
            "candles": candles,
        }


class FakeVolumeProfileProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.redis_provider = FakeIndicatorRedisProvider()
        self.calls = []

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size):
        self.calls.append({
            "symbol": symbol,
            "fromTime": from_time,
            "toTime": to_time,
            "priceBinSize": price_bin_size,
        })
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "priceBinSize": 0.25,
            "source": "unit",
            "feed": "sip",
            "bins": [
                {"priceBin": 100.0, "priceBinSize": 0.25, "volume": 10, "tradeCount": 1, "vwap": 100.1},
                {"priceBin": 100.5, "priceBinSize": 0.25, "volume": 45, "tradeCount": 4, "vwap": 100.6},
                {"priceBin": 101.0, "priceBinSize": 0.25, "volume": 25, "tradeCount": 3, "vwap": 101.1},
            ],
        }


class FakeFootprintProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.redis_provider = FakeIndicatorRedisProvider()
        self.calls = []

    def footprint_ticks(self, symbol, from_time, to_time, limit=20000):
        self.calls.append({
            "symbol": symbol,
            "fromTime": from_time,
            "toTime": to_time,
            "limit": limit,
        })
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "source": "unit",
            "feed": "sip",
            "quotes": [
                {"timestamp": "2026-06-25T13:30:00.000Z", "bidPrice": 100.0, "askPrice": 100.1},
            ],
            "trades": [
                {"timestamp": "2026-06-25T13:30:01.000Z", "price": 100.1, "size": 10},
                {"timestamp": "2026-06-25T13:30:02.000Z", "price": 100.0, "size": 4},
            ],
        }


class FakeNewsRedisProvider:
    def __init__(self, rows=None, daily_rows=None, daily_coverage=None):
        self.rows = rows or []
        self.daily_rows = daily_rows or []
        self.daily_coverage = daily_coverage
        self.localized_calls = []
        self.daily_calls = []
        self.localized_warm_calls = []
        self.daily_warm_calls = []

    def localized_news_articles_for_symbols(self, symbols, limit=10, locale="ko-KR"):
        self.localized_calls.append({"symbols": list(symbols), "limit": limit, "locale": locale})
        return self.rows[:limit]

    def warm_localized_news_articles(self, rows, locale="ko-KR"):
        self.localized_warm_calls.append({"rows": list(rows), "locale": locale})
        self.rows = list(rows)
        return len(rows)

    def company_daily_news_summaries(self, symbol, limit=5, locale="ko-KR"):
        self.daily_calls.append({"symbol": symbol, "limit": limit, "locale": locale})
        return self.daily_rows[:limit]

    def company_daily_news_coverage(self, symbol, locale="ko-KR"):
        return self.daily_coverage

    def warm_company_daily_news_summaries(self, symbol, rows, days=30, limit=30, locale="ko-KR"):
        self.daily_warm_calls.append({"symbol": symbol, "rows": list(rows), "days": days, "limit": limit, "locale": locale})
        self.daily_rows = list(rows)[:limit]
        self.daily_coverage = {
            "symbol": symbol,
            "locale": locale,
            "days": days,
            "limit": limit,
            "rowCount": len(self.daily_rows),
            "coverageType": "complete",
        }
        return self.daily_rows


class FakeNewsClickHouseProvider:
    def __init__(self, rows=None, daily_rows=None, candles=None, ranking_rows_by_kind=None):
        self.rows = rows or []
        self.daily_rows = daily_rows or []
        self.candle_rows = candles or []
        self.ranking_rows_by_kind = ranking_rows_by_kind or {}
        self.localized_calls = []
        self.daily_calls = []
        self.ranking_calls = []

    def localized_news_articles_for_symbols(self, symbols, limit=10, days=7, locale="ko-KR"):
        self.localized_calls.append({"symbols": list(symbols), "limit": limit, "days": days, "locale": locale})
        return self.rows[:limit]

    def company_daily_news_summaries(self, symbol, limit=5, days=30, locale="ko-KR"):
        self.daily_calls.append({"symbol": symbol, "limit": limit, "days": days, "locale": locale})
        return self.daily_rows[:limit]

    def candles(self, symbol, interval, limit):
        return self.candle_rows[-limit:]

    def rank_symbols(self, symbols, kind="dollar-volume", limit=10):
        self.ranking_calls.append({"symbols": list(symbols), "kind": kind, "limit": limit})
        allowed = set(symbols)
        rows = [
            row for row in self.ranking_rows_by_kind.get(kind, [])
            if row.get("symbol") in allowed
        ]
        return rows[:limit]


class FakeNewsProvider:
    def __init__(self, redis_rows=None, clickhouse_rows=None, redis_daily_rows=None, clickhouse_daily_rows=None, candle_rows=None, redis_daily_coverage=None, ranking_rows_by_kind=None):
        self.redis_provider = FakeNewsRedisProvider(redis_rows, redis_daily_rows, redis_daily_coverage)
        self.clickhouse_provider = FakeNewsClickHouseProvider(clickhouse_rows, clickhouse_daily_rows, candle_rows, ranking_rows_by_kind)

    def symbol_detail(self, symbol):
        names = {
            "NVDA": "NVIDIA Corporation",
            "AMD": "Advanced Micro Devices, Inc.",
            "AAPL": "Apple Inc.",
        }
        return {"symbol": symbol, "name": names.get(symbol, symbol), "market": "NASDAQ"}


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
        self.lists = {}
        self.expirations = {}

    def delete(self, key):
        self.sets.pop(key, None)
        self.hashes.pop(key, None)
        self.values.pop(key, None)
        self.lists.pop(key, None)
        return 1

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(value)
        return 1

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def lrange(self, key, start, end):
        values = list(self.lists.get(key, []))
        if end == -1:
            return values[start:]
        return values[start:end + 1]

    def hset(self, key, mapping=None, **kwargs):
        values = mapping or kwargs
        self.hashes.setdefault(key, {}).update(values)
        return len(values)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
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


class FakeHeatmapClickHouseProvider:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {
                "symbol": "AAPL",
                "lastPrice": 200,
                "changePercent": 1.25,
                "volume": 1000,
                "sessionDollarVolume": 200000,
                "sourceUpdatedAt": "2026-06-25T15:31:00.000Z",
                "rankReason": "clickhouse_1m_session_aggregate",
            },
            {
                "symbol": "MSFT",
                "lastPrice": 300,
                "changePercent": -2.5,
                "sourceUpdatedAt": "2026-06-25T15:31:00.000Z",
                "rankReason": "clickhouse_1m_session_aggregate",
            },
        ]
        self.calls = []

    def rank_symbols(self, symbols, kind="dollar-volume", limit=10):
        self.calls.append({"symbols": list(symbols), "kind": kind, "limit": limit})
        allowed = set(symbols)
        return [row for row in self.rows if row.get("symbol") in allowed][:limit]

    def latest_quotes(self, symbols, limit=None):
        self.calls.append({"symbols": list(symbols), "limit": limit, "method": "latest_quotes"})
        allowed = set(symbols)
        rows = [row for row in self.rows if row.get("symbol") in allowed]
        return rows[:limit] if limit is not None else rows

    def table(self, name):
        return f"market_data.{name}"

    def query_json_each_row(self, query, params):
        self.calls.append({"query": query, "params": dict(params)})
        if "sec_company_tickers" in query:
            return [
                {"symbol": "MSFT", "cik": "0000789019", "companyName": "Microsoft Corporation"},
            ]
        if "sec_derived_metrics" in query:
            return [
                {
                    "symbol": "MSFT",
                    "metric": "free_cash_flow",
                    "value": 25000,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "filedAt": "2026-04-25",
                },
            ]
        if "yahoo_earnings_estimates" in query:
            return [
                {
                    "symbol": "MSFT",
                    "metric": "eps",
                    "value": 4.5,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "collectedAt": "2026-04-20T00:00:00Z",
                },
                {
                    "symbol": "MSFT",
                    "metric": "revenue",
                    "value": 95000,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "collectedAt": "2026-04-20T00:00:00Z",
                },
            ]
        if "sec_financial_facts" in query:
            return [
                {
                    "symbol": "MSFT",
                    "cik": "0000789019",
                    "metric": "shares_outstanding",
                    "value": 7500,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "filedAt": "2026-04-25",
                },
                {
                    "symbol": "MSFT",
                    "cik": "0000789019",
                    "metric": "revenue",
                    "value": 100000,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "filedAt": "2026-04-25",
                },
                {
                    "symbol": "MSFT",
                    "cik": "0000789019",
                    "metric": "equity",
                    "value": 50000,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "filedAt": "2026-04-25",
                },
                {
                    "symbol": "MSFT",
                    "cik": "0000789019",
                    "metric": "eps",
                    "value": 4,
                    "fiscalYear": 2026,
                    "fiscalPeriod": "Q1",
                    "periodEndDate": "2026-03-31",
                    "filedAt": "2026-04-25",
                },
            ]
        return []


class FakeHeatmapRedisProvider:
    def __init__(self, prices=None):
        self.redis = FakeWatchlistRedis()
        self.prices = prices or {}

    def latest_price(self, symbol):
        return self.prices.get(symbol) or {}


class FakeHeatmapProvider:
    def __init__(self, rows=None, redis_prices=None):
        self.redis_provider = FakeHeatmapRedisProvider(redis_prices)
        self.clickhouse_provider = FakeHeatmapClickHouseProvider(rows)

    def symbol_detail(self, symbol):
        return {"symbol": symbol, "name": symbol, "market": "NASDAQ"}


class FakeFundamentalsAdapter(FundamentalsAdapter):
    def __init__(self, records):
        self.records = records

    def latest_for_symbols(self, symbols):
        requested = set(symbols)
        return {symbol: record for symbol, record in self.records.items() if symbol in requested}


class EmptyFakeProvider(FakeProvider):
    def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None, ma_windows=None):
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
                "message": None,
            }
        return {
            "dataStatus": "empty",
            "sourceInterval": interval,
            "message": f"No stored {interval} candles were found for {symbol}.",
            "coverage": {
                "state": "empty",
                "reasonCode": "no_stored_candles",
                "sourceInterval": interval,
                "returnedCount": 0,
            },
        }

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default", force=False):
        raise HTTPException(status_code=410, detail="Backfill queue endpoints were replaced by on-demand fill.")

    def get_status(self, symbol, interval, request_id=None):
        raise HTTPException(status_code=410, detail="Backfill status was replaced by on-demand fill trace.")

    def queue_metrics(self):
        raise HTTPException(status_code=410, detail="Backfill queue metrics were replaced by on-demand fill trace.")


class FakeFillService:
    def fill_if_needed(self, *, symbol, interval, limit, before, from_time, to_time, payload):
        returned = len(payload.get("candles") or [])
        payload["fill"] = {
            "status": "not_needed" if returned else "empty",
            "requestedLimit": limit,
            "sourceInterval": interval,
            "sources": {
                "redis": {"checked": False, "hit": False, "rowCount": 0, "durationMs": 0, "error": None},
                "clickhouse": {"checked": True, "hit": returned > 0, "rowCount": returned, "durationMs": 0, "error": None},
                "s3": {"checked": False, "hit": False, "rowCount": 0, "durationMs": 0, "error": None},
                "alpaca": {"checked": False, "hit": False, "rowCount": 0, "durationMs": 0, "error": None},
            },
            "missingRanges": [],
            "gapRanges": [],
            "renderable": returned > 0,
        }
        return payload


def make_fill_candles(count):
    return [
        {"timestamp": f"2026-06-25T13:{index:02d}:00.000Z", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        for index in range(count)
    ]


class FakeDerivedClient:
    def __init__(self, redis_client=None):
        self.requests = []
        self.redis_client = redis_client

    def resolve(self, request):
        self.requests.append(request)
        if request["kind"] == "indicators":
            layers = str((request.get("parameters") or {}).get("layers") or "")
            series = {layer: [] for layer in layers.split(",") if layer}
            return {
                "symbol": request["symbol"],
                "interval": request["interval"],
                "calculationVersion": request["calculationVersion"],
                "dataStatus": "ready",
                "indicators": [{"id": layer, "kind": layer.split(":")[0], "placement": "overlay", "parameters": {}, "points": []} for layer in series],
                "series": series,
                "cache": {"hit": False, "ttlSeconds": 300, "keyVersion": request["calculationVersion"]},
                "derived": {"state": "ready", "source": "worker", "requestHash": request["requestHash"], "artifactStored": True},
            }
        if request["kind"] == "volumeProfile":
            return {
                "symbol": request["symbol"],
                "interval": request["interval"],
                "sourceInterval": request["interval"],
                "from": request["from"],
                "to": request["to"],
                "timeBucket": request["interval"],
                "targetBins": int((request.get("parameters") or {}).get("targetBins") or 10),
                "bucketCount": 0,
                "priceBinSize": 0,
                "sourceBinCount": 0,
                "source": "worker",
                "feed": "test",
                "calculationVersion": request["calculationVersion"],
                "classificationVersion": request["calculationVersion"],
                "sideClassification": "estimated",
                "estimationMethod": "candle-range-volume-overlap",
                "dataStatus": "ready",
                "priceRange": {"min": None, "max": None, "requestedMin": None, "requestedMax": None},
                "totalVolume": 0,
                "totalTradeCount": 0,
                "bins": [],
                "poc": None,
                "valueArea": None,
                "cache": {"hit": False, "ttlSeconds": 30, "keyVersion": request["calculationVersion"]},
                "derived": {"state": "ready", "source": "worker", "requestHash": request["requestHash"], "artifactStored": True},
            }
        return {
            "symbol": request["symbol"],
            "interval": "footprint",
            "sourceInterval": "1m",
            "from": request["from"],
            "to": request["to"],
            "timeBucket": "1m",
            "source": "worker",
            "feed": "test",
            "dataStatus": "ready",
            "sideClassification": "estimated",
            "classificationVersion": request["calculationVersion"],
            "calculationVersion": request["calculationVersion"],
            "tradeCount": 0,
            "quoteCount": 0,
            "requestedLimit": request.get("limit"),
            "buckets": [],
            "cache": {"hit": False, "ttlSeconds": 15, "keyVersion": request["calculationVersion"]},
            "derived": {"state": "ready", "source": "worker", "requestHash": request["requestHash"], "artifactStored": True},
        }


class RecordingOnDemandFillService(OnDemandFillService):
    def __init__(self, *, provider=None, s3_result=False, alpaca_result=False):
        super().__init__(provider=provider, timeout_seconds=8, background_enabled=False)
        self.foreground_enabled = False
        self.s3_result = s3_result
        self.alpaca_result = alpaca_result
        self.calls = []
        self.queued = []

    def _enqueue_background_fill(self, **kwargs):
        self.queued.append(kwargs)
        return {"queued": True, "state": "queued", "requestId": "test-fill", "reason": "range repair queued"}

    def _fill_from_s3(self, symbol, interval, ranges, trace, started):
        self.calls.append(("s3", symbol, interval, list(ranges)))
        trace["sources"]["s3"].update({"checked": True, "hit": self.s3_result, "rowCount": 30 if self.s3_result else 0})
        return self.s3_result

    def _fill_from_alpaca(self, symbol, interval, ranges, trace, started):
        self.calls.append(("alpaca", symbol, interval, list(ranges)))
        trace["sources"]["alpaca"].update({"checked": True, "hit": self.alpaca_result, "rowCount": 30 if self.alpaca_result else 0})
        return self.alpaca_result


class DeadlineAfterAlpacaFillService(RecordingOnDemandFillService):
    def __init__(self, *, provider=None):
        super().__init__(provider=provider, s3_result=False, alpaca_result=True)
        self.deadline_exceeded = False

    def _fill_from_alpaca(self, symbol, interval, ranges, trace, started):
        result = super()._fill_from_alpaca(symbol, interval, ranges, trace, started)
        self.deadline_exceeded = True
        return result

    def _deadline_exceeded(self, started):
        return self.deadline_exceeded


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
        self.service = MarketDataQueryService(
            provider or FakeProvider(),
            backfill_service=FakeBackfillService(),
            fill_service=FakeFillService(),
            derived_client=FakeDerivedClient(),
        )

    def symbol_search(self, query, limit):
        return self.service.symbol_search(query, limit)

    def symbol_detail(self, symbol):
        return self.service.symbol_detail(symbol)

    def volume_profile_bins(self, symbol, from_time, to_time, price_bin_size):
        return self.service.volume_profile_bins(symbol, from_time, to_time, price_bin_size)

    def indicator_series(self, symbol, interval, from_time=None, to_time=None, layers=None, limit=None):
        return self.service.indicator_series(symbol, interval, from_time, to_time, layers, limit)

    def latest_status(self, symbol=None):
        return self.service.latest_status(symbol)

    def agent_chart_context(self, symbol, interval, from_time, to_time, include):
        return self.service.agent_chart_context(symbol, interval, from_time, to_time, include)

    def latest_news(self, symbol, limit=10, locale="ko-KR"):
        return self.service.latest_news(symbol, limit=limit, locale=locale)

    def watchlist_news(self, user_sub, limit=30, locale="ko-KR", mode="watchlist", recommendation_repository=None):
        return self.service.watchlist_news(
            user_sub,
            limit=limit,
            locale=locale,
            mode=mode,
            recommendation_repository=recommendation_repository,
        )

    def request_backfill(self, symbol, interval, start=None, end=None, mode="default", force=False):
        return self.service.request_backfill(symbol, interval, start=start, end=end, mode=mode, force=force)

    def backfill_status(self, symbol, interval, request_id=None):
        return self.service.backfill_status(symbol, interval, request_id=request_id)

    def backfill_queue_metrics(self):
        return self.service.backfill_queue_metrics()


class MarketDataQueryServiceTest(unittest.TestCase):
    def test_candle_snapshot_adds_requested_indicators_and_normalizes_symbol(self):
        provider = FakeProvider()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        payload = service.candle_snapshot("aapl", "1m", "5,60,999", None)

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(provider.last_limit, 120)
        self.assertEqual(provider.last_ma_windows, (5, 60))
        self.assertEqual(payload["indicators"], {"ma": [5, 60], "volume": True})

    def test_candle_snapshot_omits_moving_averages_by_default(self):
        class MovingAverageProvider(FakeProvider):
            def candle_snapshot(self, symbol, interval, limit, before=None, from_time=None, to_time=None, ma_windows=None):
                self.last_limit = limit
                self.last_ma_windows = ma_windows
                return {
                    "symbol": symbol,
                    "interval": interval,
                    "snapshotCursor": "cursor-1",
                    "candles": [{
                        "timestamp": "2026-06-25T10:15:00.000Z",
                        "open": 99,
                        "high": 101,
                        "low": 98,
                        "close": 100,
                        "volume": 1000,
                        "ma5": 99.5,
                        "ma20": 98.5,
                        "ma60": 97.5,
                    }],
                }

        provider = MovingAverageProvider()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        payload = service.candle_snapshot("aapl", "1m", "", None)

        self.assertEqual(provider.last_ma_windows, ())
        self.assertEqual(payload["indicators"], {"ma": [], "volume": True})
        self.assertNotIn("ma5", payload["candles"][0])
        self.assertNotIn("ma20", payload["candles"][0])
        self.assertNotIn("ma60", payload["candles"][0])

    def test_candle_snapshot_accepts_canonical_and_legacy_intervals(self):
        provider = FakeProvider()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        daily_payload = service.candle_snapshot("aapl", "1d", "5,20,60", None)
        hourly_payload = service.candle_snapshot("aapl", "1h", "5,20,60", None)
        four_hour_payload = service.candle_snapshot("aapl", "4h", "5,20,60", None)
        weekly_payload = service.candle_snapshot("aapl", "1W", "5,20,60", None)
        monthly_payload = service.candle_snapshot("aapl", "1M", "5,20,60", None)

        self.assertEqual(hourly_payload["interval"], "1h")
        self.assertEqual(four_hour_payload["interval"], "4h")
        self.assertEqual(daily_payload["interval"], "1D")
        self.assertEqual(weekly_payload["interval"], "1W")
        self.assertEqual(monthly_payload["interval"], "1M")

    def test_candle_snapshot_provider_error_maps_to_503(self):
        service = MarketDataQueryService(FakeProvider(fail_snapshot=True), backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        with self.assertRaises(HTTPException) as raised:
            service.candle_snapshot("AAPL", "1m", "5,20,60", 30)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("Market data provider failed", str(raised.exception.detail))

    def test_query_service_routes_core_market_context(self):
        service = MarketDataQueryService(
            FakeProvider(),
            backfill_service=FakeBackfillService(),
            fill_service=FakeFillService(),
            derived_client=FakeDerivedClient(),
        )

        self.assertEqual(service.symbol_search("aa", 10)["symbols"][0]["symbol"], "AAPL")
        self.assertEqual(service.symbol_detail("aapl")["symbol"], "AAPL")
        self.assertEqual(service.latest_status("AAPL")["status"], "active")
        self.assertEqual(service.latest_status()["symbol"], "_MARKET")
        self.assertEqual(service.volume_profile_bins("aapl", "from", "to", "auto")["symbol"], "AAPL")
        context = service.agent_chart_context("aapl", "1m", "from", "to", "status,volumeProfile")
        self.assertEqual(context["include"], ["status", "volumeProfile"])

    def test_volume_profile_bins_uses_display_buckets_and_redis_cache(self):
        provider = FakeVolumeProfileProvider()
        derived_client = FakeDerivedClient()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService(), derived_client=derived_client)

        with mock.patch.dict(os.environ, {"CHART_VOLUME_PROFILE_CACHE_TTL_SECONDS": "44"}):
            payload = service.volume_profile_bins(
                "aapl",
                "2026-06-25T13:30:00.000Z",
                "2026-06-25T14:00:00.000Z",
                "auto",
                target_bins=4,
                price_min=100,
                price_max=102,
            )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["calculationVersion"], "volume-profile-v1")
        self.assertEqual(payload["targetBins"], 4)
        self.assertEqual(payload["derived"]["state"], "ready")
        self.assertEqual(provider.calls, [])
        self.assertEqual(derived_client.requests[0]["kind"], "volumeProfile")
        self.assertEqual(derived_client.requests[0]["interval"], "1m")
        self.assertEqual(derived_client.requests[0]["parameters"]["targetBins"], 4)
        self.assertEqual(derived_client.requests[0]["parameters"]["priceMin"], 100)
        self.assertEqual(derived_client.requests[0]["parameters"]["priceMax"], 102)
        payload_5m = service.volume_profile_bins(
            "aapl",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T14:00:00.000Z",
            "auto",
            target_bins=4,
            price_min=100,
            price_max=102,
            interval="5m",
        )
        self.assertEqual(payload_5m["interval"], "5m")
        self.assertNotEqual(derived_client.requests[0]["requestHash"], derived_client.requests[1]["requestHash"])
        payload_1h = service.volume_profile_bins(
            "aapl",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T17:30:00.000Z",
            "auto",
            target_bins=4,
            price_min=100,
            price_max=102,
            interval="1h",
        )
        self.assertEqual(payload_1h["interval"], "1h")
        self.assertEqual(derived_client.requests[2]["interval"], "1h")

    def test_volume_profile_bins_rejects_reversed_price_range(self):
        service = MarketDataQueryService(FakeVolumeProfileProvider(), backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        with self.assertRaises(HTTPException) as raised:
            service.volume_profile_bins("aapl", "from", "to", "auto", target_bins=10, price_min=102, price_max=100)

        self.assertEqual(raised.exception.status_code, 400)

    def test_footprint_series_uses_estimated_calculation_and_redis_cache(self):
        provider = FakeFootprintProvider()
        derived_client = FakeDerivedClient()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService(), derived_client=derived_client)

        with mock.patch.dict(os.environ, {"CHART_FOOTPRINT_CACHE_TTL_SECONDS": "22"}):
            payload = service.footprint_series(
                "aapl",
                "2026-06-25T13:30:00.000Z",
                "2026-06-25T13:31:00.000Z",
                limit=100,
            )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["interval"], "footprint")
        self.assertEqual(payload["sourceInterval"], "1m")
        self.assertEqual(payload["sideClassification"], "estimated")
        self.assertEqual(payload["derived"]["state"], "ready")
        self.assertEqual(provider.calls, [])
        self.assertEqual(derived_client.requests[0]["kind"], "footprint")
        self.assertEqual(derived_client.requests[0]["limit"], 100)

    def test_indicator_series_uses_filled_candle_snapshot_lookback_inline(self):
        provider = FakeIndicatorProvider()
        derived_client = FakeDerivedClient()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService(), derived_client=derived_client)

        payload = service.indicator_series(
            "aapl",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:39:00.000Z",
            "ma5,ema:5,rsi:14",
            30,
        )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["interval"], "1m")
        self.assertEqual([item["id"] for item in payload["indicators"]], ["sma:5", "ema:5", "rsi:14"])
        self.assertEqual(payload["derived"]["source"], "api-inline")
        self.assertFalse(payload["derived"]["artifactStored"])
        self.assertEqual(len(payload["series"]["sma:5"]), 10)
        self.assertIsNotNone(payload["series"]["sma:5"][0]["value"])
        self.assertEqual(provider.calls[0]["before"], "2026-06-25T13:30:00.000Z")
        self.assertEqual(provider.calls[0]["limit"], 15)
        self.assertEqual(provider.calls[0]["maWindows"], ())
        self.assertEqual(provider.calls[1]["fromTime"], "2026-06-25T13:30:00.000Z")
        self.assertEqual(provider.calls[1]["limit"], 30)
        self.assertEqual(provider.calls[1]["maWindows"], ())
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(derived_client.requests, [])

    def test_indicator_series_uses_foreground_fill_candles(self):
        class DirectFillService(FakeFillService):
            def fill_if_needed(self, *, symbol, interval, limit, before, from_time, to_time, payload):
                if before:
                    end = datetime.fromisoformat(before.replace("Z", "+00:00"))
                    start = end - timedelta(hours=limit)
                else:
                    start = datetime.fromisoformat((from_time or "2026-06-25T13:00:00.000Z").replace("Z", "+00:00"))
                candles = []
                for index in range(limit):
                    timestamp = start + timedelta(hours=index)
                    close = 100 + index
                    candles.append({
                        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "open": close - 0.2,
                        "high": close + 1,
                        "low": close - 1,
                        "close": close,
                        "volume": 1000 + index,
                        "sourceInterval": interval,
                    })
                payload.update({
                    "source": "alpaca",
                    "feed": "sip",
                    "dataStatus": "ready",
                    "candles": candles,
                })
                return super().fill_if_needed(
                    symbol=symbol,
                    interval=interval,
                    limit=limit,
                    before=before,
                    from_time=from_time,
                    to_time=to_time,
                    payload=payload,
                )

        derived_client = FakeDerivedClient()
        service = MarketDataQueryService(
            EmptyFakeProvider(),
            backfill_service=FakeBackfillService(),
            fill_service=DirectFillService(),
            derived_client=derived_client,
        )

        payload = service.indicator_series(
            "bac",
            "1h",
            "2026-06-25T13:00:00.000Z",
            "2026-06-25T20:00:00.000Z",
            "sma:5",
            8,
        )

        self.assertEqual(payload["symbol"], "BAC")
        self.assertEqual(payload["interval"], "1h")
        self.assertEqual(payload["derived"]["source"], "api-inline")
        self.assertEqual(payload["returnedCandleCount"], 13)
        self.assertEqual(len(payload["series"]["sma:5"]), 8)
        self.assertIsNotNone(payload["series"]["sma:5"][0]["value"])
        self.assertEqual(derived_client.requests, [])

    def test_indicator_series_reuses_inline_redis_cache(self):
        provider = FakeIndicatorProvider()
        redis_client = FakeIndicatorRedis()
        derived_client = FakeDerivedClient(redis_client)
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService(), fill_service=FakeFillService(), derived_client=derived_client)

        first = service.indicator_series(
            "aapl",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:39:00.000Z",
            "ema:5,rsi:14",
            30,
        )
        provider_call_count = len(provider.calls)
        second = service.indicator_series(
            "aapl",
            "1m",
            "2026-06-25T13:30:00.000Z",
            "2026-06-25T13:39:00.000Z",
            "ema:5,rsi:14",
            30,
        )

        self.assertEqual(first["derived"]["source"], "api-inline")
        self.assertEqual(second["derived"]["source"], "redis")
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(len(provider.calls), provider_call_count)
        self.assertEqual(derived_client.requests, [])

    def test_indicator_series_rejects_unsupported_layer(self):
        service = MarketDataQueryService(FakeIndicatorProvider(), backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        with self.assertRaises(HTTPException) as raised:
            service.indicator_series("AAPL", "1m", layers="unknown:10", limit=30)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Unsupported indicator layer", str(raised.exception.detail))

    def test_indicator_route_delegates_to_query_service(self):
        previous = query_routes.get_query_service
        query_routes.get_query_service = lambda: FakeQueryService(FakeIndicatorProvider())
        try:
            payload = query_routes.chart_indicators("aapl", "1m", None, None, "sma:5", 10)
        finally:
            query_routes.get_query_service = previous

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertIn("sma:5", payload["series"])

    @unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient dependency is not installed")
    def test_indicator_route_accepts_large_client_limit_for_service_clamp(self):
        from app.main import create_app

        class LargeLimitQueryService:
            def indicator_series(self, symbol, interval, from_time=None, to_time=None, layers=None, limit=None):
                return {
                    "symbol": symbol.upper(),
                    "interval": interval,
                    "from": from_time,
                    "to": to_time,
                    "layers": layers,
                    "limit": limit,
                    "series": {},
                }

        previous = query_routes.get_query_service
        query_routes.get_query_service = lambda: LargeLimitQueryService()
        try:
            client = TestClient(create_app())
            response = client.get(
                "/api/charts/indicators?symbol=nvda&interval=1D&layers=sma:5&limit=36477"
                "&from=2026-07-02T04:00:00.000Z&to=2026-07-08T03:40:49.000Z"
            )
        finally:
            query_routes.get_query_service = previous

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 36477)

    def test_latest_news_uses_redis_when_cache_has_enough_rows(self):
        service = MarketDataQueryService(FakeNewsProvider(redis_rows=[{
            "targetSymbol": "NVDA",
            "symbols": ["NVDA"],
            "localizedHeadline": "Redis 엔비디아 뉴스",
            "localizedSummary": "Redis hot cache 요약입니다.",
            "publishedAt": "2026-07-02T12:00:00.000Z",
        }], clickhouse_rows=[{
            "targetSymbol": "NVDA",
            "symbols": ["NVDA"],
            "localizedHeadline": "ClickHouse 엔비디아 뉴스",
            "localizedSummary": "ClickHouse 요약입니다.",
            "publishedAt": "2026-07-01T12:00:00.000Z",
        }]), backfill_service=FakeBackfillService())

        payload = service.latest_news("nvda", limit=1)

        self.assertEqual(payload["source"], "redis")
        self.assertEqual(payload["items"][0]["title"], "Redis 엔비디아 뉴스")
        self.assertEqual(service.provider.redis_provider.localized_calls[0]["limit"], 1)
        self.assertEqual(service.provider.clickhouse_provider.localized_calls, [])

    def test_latest_news_uses_clickhouse_when_redis_cache_is_insufficient(self):
        service = MarketDataQueryService(FakeNewsProvider(redis_rows=[{
            "targetSymbol": "NVDA",
            "symbols": ["NVDA"],
            "localizedHeadline": "Redis 엔비디아 뉴스",
            "localizedSummary": "Redis hot cache 요약입니다.",
            "publishedAt": "2026-07-02T12:00:00.000Z",
        }], clickhouse_rows=[{
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
        self.assertEqual(payload["source"], "clickhouse")
        self.assertEqual(payload["items"][0]["title"], "엔비디아 최신 뉴스")
        self.assertEqual(payload["items"][0]["summary"], "데이터센터 수요가 강합니다.")
        self.assertEqual(payload["items"][0]["impactDirection"], "positive")
        self.assertEqual(service.provider.redis_provider.localized_calls[0]["limit"], 5)
        self.assertEqual(service.provider.clickhouse_provider.localized_calls[0]["days"], 30)
        self.assertEqual(len(service.provider.redis_provider.localized_warm_calls[0]["rows"]), 1)

    def test_latest_news_prefers_localized_text_when_raw_text_is_present(self):
        service = MarketDataQueryService(FakeNewsProvider(clickhouse_rows=[{
            "targetSymbol": "NVDA",
            "symbols": ["NVDA"],
            "headline": "NVIDIA English headline",
            "localizedHeadline": "엔비디아 한국어 제목",
            "summary": "English raw summary should not be displayed.",
            "localizedSummary": "한국어 번역 요약이 먼저 표시되어야 합니다.",
            "publishedAt": "2026-07-01T12:00:00.000Z",
        }]), backfill_service=FakeBackfillService())

        payload = service.latest_news("nvda", limit=5)

        self.assertEqual(payload["items"][0]["title"], "엔비디아 한국어 제목")
        self.assertEqual(payload["items"][0]["summary"], "한국어 번역 요약이 먼저 표시되어야 합니다.")

    def test_latest_news_falls_back_to_redis_when_clickhouse_empty(self):
        service = MarketDataQueryService(FakeNewsProvider(redis_rows=[{
            "target_symbol": "AAPL",
            "headline": "Apple headline",
            "summary": "Apple summary",
            "published_at": "2026-07-01T12:00:00.000Z",
        }]), backfill_service=FakeBackfillService())

        payload = service.latest_news("aapl", limit=5)

        self.assertEqual(payload["source"], "redis")
        self.assertEqual(payload["items"][0]["symbol"], "AAPL")
        self.assertEqual(payload["items"][0]["title"], "Apple headline")
        self.assertEqual(service.provider.redis_provider.localized_calls[0]["limit"], 5)
        self.assertEqual(service.provider.clickhouse_provider.localized_calls[0]["days"], 30)

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

    def test_watchlist_news_returns_empty_payload_for_empty_watchlist(self):
        provider = FakeNewsProvider()
        provider.redis_provider.redis = FakeWatchlistRedis()
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        payload = service.watchlist_news("user-a", limit=10)

        self.assertEqual(payload["displayMode"], "watchlistNews")
        self.assertEqual(payload["symbols"], [])
        self.assertEqual(payload["items"], [])
        self.assertIn("관심종목", payload["message"])
        self.assertEqual(provider.redis_provider.localized_calls, [])
        self.assertEqual(provider.clickhouse_provider.localized_calls, [])

    def test_watchlist_news_uses_batch_lookup_and_dedupes_articles(self):
        provider = FakeNewsProvider(clickhouse_rows=[
            {
                "articleId": "shared-1",
                "targetSymbol": "NVDA",
                "symbols": ["NVDA", "AMD"],
                "localizedHeadline": "반도체 수요 뉴스",
                "localizedSummary": "엔비디아와 AMD가 함께 언급됐습니다.",
                "url": "https://example.com/shared",
                "publishedAt": "2026-07-02T12:00:00.000Z",
                "impactDirection": "positive",
            },
            {
                "articleId": "shared-1",
                "targetSymbol": "AMD",
                "symbols": ["NVDA", "AMD"],
                "localizedHeadline": "반도체 수요 뉴스",
                "localizedSummary": "중복 기사입니다.",
                "url": "https://example.com/shared",
                "publishedAt": "2026-07-02T12:00:00.000Z",
            },
            {
                "articleId": "amd-2",
                "targetSymbol": "AMD",
                "symbols": ["AMD"],
                "headline": "AMD 신제품 뉴스",
                "summary": "신제품 출시 일정입니다.",
                "publishedAt": "2026-07-01T12:00:00.000Z",
            },
        ])
        provider.redis_provider.redis = FakeWatchlistRedis()
        from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService

        RealtimeSubscriptionCohortService(provider.redis_provider.redis, auto_reconcile=False).replace_user_watchlist("user-a", ["NVDA", "AMD"])
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        payload = service.watchlist_news("user-a", limit=10)

        self.assertEqual(payload["source"], "clickhouse")
        self.assertEqual(payload["symbols"], ["NVDA", "AMD"])
        self.assertEqual([item["articleId"] for item in payload["items"]], ["shared-1", "amd-2"])
        self.assertEqual([match["symbol"] for match in payload["items"][0]["matches"]], ["NVDA", "AMD"])
        self.assertEqual(payload["items"][0]["matches"][0]["companyName"], "NVIDIA Corporation")
        self.assertEqual(provider.clickhouse_provider.localized_calls[0]["symbols"], ["NVDA", "AMD"])
        self.assertEqual(provider.clickhouse_provider.localized_calls[0]["days"], 30)

    def test_watchlist_news_hot_mode_uses_ranked_symbols(self):
        provider = FakeNewsProvider(clickhouse_rows=[
            {
                "articleId": "nvda-hot",
                "targetSymbol": "NVDA",
                "symbols": ["NVDA"],
                "localizedHeadline": "엔비디아 인기 뉴스",
                "localizedSummary": "급등 종목 관련 뉴스입니다.",
                "publishedAt": "2026-07-03T12:00:00.000Z",
            },
            {
                "articleId": "amd-hot",
                "targetSymbol": "AMD",
                "symbols": ["AMD"],
                "localizedHeadline": "AMD 인기 뉴스",
                "localizedSummary": "급락 종목 관련 뉴스입니다.",
                "publishedAt": "2026-07-02T12:00:00.000Z",
            },
            {
                "articleId": "aapl-hot",
                "targetSymbol": "AAPL",
                "symbols": ["AAPL"],
                "localizedHeadline": "애플 인기 뉴스",
                "localizedSummary": "거래대금 상위 종목 관련 뉴스입니다.",
                "publishedAt": "2026-07-01T12:00:00.000Z",
            },
        ], ranking_rows_by_kind={
            "gainers": [{"symbol": "NVDA"}],
            "losers": [{"symbol": "AMD"}],
            "dollar-volume": [{"symbol": "AAPL"}],
        })
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        with mock.patch.object(query_service_module, "sp500_universe_symbols", return_value=["NVDA", "AMD", "AAPL", "MSFT"]):
            payload = service.watchlist_news("user-a", limit=10, mode="hot")

        self.assertEqual(payload["displayMode"], "hotNews")
        self.assertEqual(payload["symbols"], ["NVDA", "AMD", "AAPL"])
        self.assertEqual(provider.clickhouse_provider.localized_calls[0]["symbols"], ["NVDA", "AMD", "AAPL"])
        reasons = {item["symbol"]: item["matches"][0]["reason"] for item in payload["items"]}
        self.assertEqual(reasons["NVDA"], "급등")
        self.assertEqual(reasons["AMD"], "급락")
        self.assertEqual(reasons["AAPL"], "거래대금")

    def test_watchlist_news_recommended_mode_reads_latest_recommendation_symbols(self):
        provider = FakeNewsProvider(clickhouse_rows=[
            {
                "articleId": "msft-rec",
                "targetSymbol": "MSFT",
                "symbols": ["MSFT"],
                "localizedHeadline": "마이크로소프트 추천 뉴스",
                "localizedSummary": "추천 기업 관련 뉴스입니다.",
                "publishedAt": "2026-07-03T12:00:00.000Z",
            },
            {
                "articleId": "avgo-rec",
                "targetSymbol": "AVGO",
                "symbols": ["AVGO"],
                "localizedHeadline": "브로드컴 추천 뉴스",
                "localizedSummary": "추천 기업 관련 뉴스입니다.",
                "publishedAt": "2026-07-02T12:00:00.000Z",
            },
        ])
        repository = types.SimpleNamespace(
            calls=[],
            latest_run=lambda user_sub: {
                "items": [
                    {"symbol": "MSFT", "rank": 1},
                    {"symbol": "AVGO", "rank": 2},
                ]
            },
        )
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        payload = service.watchlist_news("user-a", limit=10, mode="recommended", recommendation_repository=repository)

        self.assertEqual(payload["displayMode"], "recommendedNews")
        self.assertEqual(payload["symbols"], ["MSFT", "AVGO"])
        self.assertEqual(provider.clickhouse_provider.localized_calls[0]["symbols"], ["MSFT", "AVGO"])
        self.assertEqual(payload["items"][0]["matches"][0]["reason"], "추천")

    def test_watchlist_news_recommended_mode_is_empty_without_latest_run(self):
        provider = FakeNewsProvider()
        repository = types.SimpleNamespace(latest_run=lambda user_sub: None)
        service = MarketDataQueryService(provider, backfill_service=FakeBackfillService())

        payload = service.watchlist_news("user-a", limit=10, mode="recommended", recommendation_repository=repository)

        self.assertEqual(payload["displayMode"], "recommendedNews")
        self.assertEqual(payload["symbols"], [])
        self.assertEqual(payload["items"], [])
        self.assertIn("추천 기업", payload["message"])
        self.assertEqual(provider.redis_provider.localized_calls, [])
        self.assertEqual(provider.clickhouse_provider.localized_calls, [])

    def test_watchlist_news_route_delegates_authenticated_user(self):
        provider = FakeNewsProvider(redis_rows=[{
            "articleId": "redis-1",
            "targetSymbol": "AAPL",
            "symbols": ["AAPL"],
            "headline": "Apple watchlist news",
            "summary": "Watchlist summary",
            "publishedAt": "2026-07-02T12:00:00.000Z",
        }])
        provider.redis_provider.redis = FakeWatchlistRedis()
        from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService

        RealtimeSubscriptionCohortService(provider.redis_provider.redis, auto_reconcile=False).replace_user_watchlist("user-a", ["AAPL"])
        previous = query_routes.get_query_service
        query_routes.get_query_service = lambda: FakeQueryService(provider)
        try:
            payload = query_routes.market_watchlist_news(
                request=types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace())),
                limit=3,
                user=AuthenticatedUser(sub="user-a", email="user@example.com", email_verified=True),
            )
        finally:
            query_routes.get_query_service = previous

        self.assertEqual(payload["source"], "redis")
        self.assertEqual(payload["symbols"], ["AAPL"])
        self.assertEqual(payload["items"][0]["title"], "Apple watchlist news")

    def test_daily_news_uses_redis_when_thirty_day_coverage_is_valid(self):
        service = MarketDataQueryService(FakeNewsProvider(redis_daily_rows=[{
            "date": "2026-07-01",
            "symbol": "NVDA",
            "summary": "Redis 30일 coverage 요약입니다.",
            "articleIds": ["redis-daily-1"],
            "articleCount": 1,
        }], redis_daily_coverage={
            "symbol": "NVDA",
            "locale": "ko-KR",
            "days": 30,
            "limit": 30,
            "rowCount": 1,
            "coverageType": "complete",
        }, clickhouse_daily_rows=[{
            "date": "2026-07-01",
            "symbol": "NVDA",
            "summary": "ClickHouse 요약입니다.",
        }]), backfill_service=FakeBackfillService())

        payload = service.daily_news("nvda", limit=30)

        self.assertEqual(payload["dailySummaries"][0]["summary"], "Redis 30일 coverage 요약입니다.")
        self.assertEqual(service.provider.redis_provider.daily_calls[0]["limit"], 30)
        self.assertEqual(service.provider.clickhouse_provider.daily_calls, [])

    def test_daily_news_warms_redis_from_clickhouse_when_coverage_is_missing(self):
        clickhouse_daily_rows = [
            {
                "date": f"2026-07-{day:02d}",
                "symbol": "NVDA",
                "summary": f"엔비디아 {day}일 뉴스 요약입니다.",
                "keyPoints": ["데이터센터 수요"],
                "articleIds": [f"nvda-daily-{day}"],
                "articleCount": 1,
                "sources": [
                    {
                        "articleId": f"nvda-daily-{day}",
                        "title": "NVIDIA shares rise",
                        "name": "Example News",
                        "url": "https://example.com/nvda-daily",
                        "publishedAt": f"2026-07-{day:02d}T12:00:00.000Z",
                    }
                ],
            }
            for day in range(1, 31)
        ]
        service = MarketDataQueryService(FakeNewsProvider(redis_daily_rows=[{
            "date": "2026-07-31",
            "symbol": "NVDA",
            "summary": "Redis hot cache 요약입니다.",
        }], clickhouse_daily_rows=clickhouse_daily_rows, candle_rows=[
            {"timestamp": "2026-06-30T00:00:00.000Z", "close": 158.35},
            {"timestamp": "2026-07-01T00:00:00.000Z", "close": 158.50},
        ]), backfill_service=FakeBackfillService())

        payload = service.daily_news("nvda", limit=30)

        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["displayMode"], "dailySummary")
        self.assertNotIn("source", payload)
        self.assertEqual(len(payload["dailySummaries"]), 30)
        self.assertEqual(payload["dailySummaries"][0]["summary"], "엔비디아 1일 뉴스 요약입니다.")
        self.assertEqual(payload["dailySummaries"][0]["sources"][0]["url"], "https://example.com/nvda-daily")
        self.assertEqual(payload["dailySummaries"][0]["sources"][0]["name"], "Example News")
        self.assertEqual(payload["dailySummaries"][0]["priceChange"]["change"], 0.15)
        self.assertEqual(service.provider.redis_provider.daily_calls[0]["limit"], 30)
        self.assertEqual(service.provider.clickhouse_provider.daily_calls[0]["days"], 30)
        self.assertEqual(service.provider.redis_provider.daily_warm_calls[0]["days"], 30)
        self.assertEqual(len(service.provider.redis_provider.daily_warm_calls[0]["rows"]), 30)

    def test_daily_news_falls_back_to_redis_when_clickhouse_empty(self):
        service = MarketDataQueryService(FakeNewsProvider(redis_daily_rows=[{
            "date": "2026-07-01",
            "symbol": "NVDA",
            "summary": "Redis 일일 뉴스 요약입니다.",
            "articleIds": ["redis-daily-1"],
            "articleCount": 1,
        }]), backfill_service=FakeBackfillService())

        payload = service.daily_news("nvda", limit=30)

        self.assertEqual(payload["dailySummaries"][0]["summary"], "Redis 일일 뉴스 요약입니다.")
        self.assertEqual(service.provider.clickhouse_provider.daily_calls[0]["days"], 30)
        self.assertEqual(service.provider.redis_provider.daily_calls[0]["limit"], 30)

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
                "dataStatus": {"state": "ready", "candleCount": 120},
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
        service = MarketDataQueryService(EmptyFakeProvider(), backfill_service=FakeBackfillService(), fill_service=FakeFillService())

        payload = service.candle_snapshot("intc", "1m", "5,20,60", 30)

        self.assertEqual(payload["symbol"], "INTC")
        self.assertEqual(payload["candles"], [])
        self.assertEqual(payload["dataStatus"], "empty")
        self.assertEqual(payload["fill"]["status"], "empty")
        self.assertTrue(payload["fill"]["sources"]["clickhouse"]["checked"])
        self.assertIn("No stored 1m candles", payload["message"])
        self.assertEqual(payload["coverage"]["state"], "empty")
        self.assertEqual(payload["coverage"]["reasonCode"], "no_stored_candles")

    def test_on_demand_fill_stops_when_redis_has_requested_window(self):
        payload = {
            "symbol": "NVDA",
            "interval": "1m",
            "candles": make_fill_candles(30),
            "_sourceTrace": {
                "redis": {"checked": True, "hit": True, "rowCount": 30},
                "clickhouse": {"checked": False, "hit": False, "rowCount": 0},
            },
        }
        service = RecordingOnDemandFillService(s3_result=True, alpaca_result=True)

        result = service.fill_if_needed(
            symbol="NVDA",
            interval="1m",
            limit=20,
            before="2026-06-25T14:00:00.000Z",
            from_time=None,
            to_time=None,
            payload=payload,
        )

        self.assertEqual(result["fill"]["status"], "not_needed")
        self.assertEqual(service.calls, [])
        self.assertEqual(service.queued, [])
        self.assertTrue(result["fill"]["sources"]["redis"]["hit"])
        self.assertFalse(result["fill"]["sources"]["s3"]["checked"])
        self.assertFalse(result["fill"]["sources"]["alpaca"]["checked"])

    def test_on_demand_fill_stops_when_clickhouse_has_requested_window(self):
        payload = {
            "symbol": "AMD",
            "interval": "1m",
            "candles": make_fill_candles(30),
            "_sourceTrace": {
                "redis": {"checked": True, "hit": False, "rowCount": 0},
                "clickhouse": {"checked": True, "hit": True, "rowCount": 30},
            },
        }
        service = RecordingOnDemandFillService(s3_result=True, alpaca_result=True)

        result = service.fill_if_needed(
            symbol="AMD",
            interval="1m",
            limit=20,
            before="2026-06-25T14:00:00.000Z",
            from_time=None,
            to_time=None,
            payload=payload,
        )

        self.assertEqual(result["fill"]["status"], "not_needed")
        self.assertEqual(service.calls, [])
        self.assertEqual(service.queued, [])
        self.assertTrue(result["fill"]["sources"]["clickhouse"]["hit"])
        self.assertFalse(result["fill"]["sources"]["s3"]["checked"])
        self.assertFalse(result["fill"]["sources"]["alpaca"]["checked"])

    def test_on_demand_fill_queues_background_when_returned_window_is_sparse_at_limit(self):
        early_start = datetime.fromisoformat("2026-06-25T13:30:00+00:00")
        late_start = datetime.fromisoformat("2026-06-25T18:00:00+00:00")
        candles = [
            {"timestamp": (early_start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(10)
        ] + [
            {"timestamp": (late_start + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            for index in range(10)
        ]
        payload = {
            "symbol": "AMD",
            "interval": "1m",
            "candles": candles,
            "returnedCount": 20,
            "storedCandleCount": 30,
            "_sourceTrace": {
                "redis": {"checked": True, "hit": False, "rowCount": 0},
                "clickhouse": {"checked": True, "hit": True, "rowCount": 20},
            },
        }
        service = RecordingOnDemandFillService(s3_result=False, alpaca_result=True)

        result = service.fill_if_needed(
            symbol="AMD",
            interval="1m",
            limit=20,
            before="2026-06-25T19:00:00.000Z",
            from_time=None,
            to_time=None,
            payload=payload,
        )

        self.assertEqual(result["fill"]["status"], "partial")
        self.assertEqual(service.calls, [])
        self.assertEqual(len(service.queued), 1)
        self.assertTrue(result["fill"]["backgroundFill"]["queued"])
        self.assertFalse(result["fill"]["sources"]["s3"]["checked"])
        self.assertFalse(result["fill"]["sources"]["alpaca"]["checked"])

    def test_on_demand_fill_disables_foreground_alpaca_by_default(self):
        payload = {
            "symbol": "BAC",
            "interval": "1h",
            "candles": [],
            "returnedCount": 0,
            "storedCandleCount": 0,
            "sourceInterval": "1m",
            "_sourceTrace": {
                "redis": {"checked": True, "hit": False, "rowCount": 0},
                "clickhouse": {"checked": True, "hit": False, "rowCount": 0},
            },
        }
        service = RecordingOnDemandFillService(s3_result=False, alpaca_result=True)
        with mock.patch.dict(os.environ, {"ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED": ""}):
            service.foreground_enabled = OnDemandFillService(timeout_seconds=8, background_enabled=False).foreground_enabled

        with mock.patch("app.market_data.fill.service.fetch_alpaca_bars") as fetch:
            result = service.fill_if_needed(
                symbol="BAC",
                interval="1h",
                limit=8,
                before=None,
                from_time="2026-06-25T13:00:00.000Z",
                to_time="2026-06-25T20:00:00.000Z",
                payload=payload,
            )

        fetch.assert_not_called()
        self.assertEqual(result["fill"]["foregroundFill"]["state"], "disabled")
        self.assertEqual(result["fill"]["status"], "empty")
        self.assertEqual(len(service.queued), 1)

    def test_on_demand_fill_uses_foreground_alpaca_direct_interval_for_missing_history(self):
        payload = {
            "symbol": "BAC",
            "interval": "1h",
            "candles": [],
            "returnedCount": 0,
            "storedCandleCount": 0,
            "sourceInterval": "1m",
            "_sourceTrace": {
                "redis": {"checked": True, "hit": False, "rowCount": 0},
                "clickhouse": {"checked": True, "hit": False, "rowCount": 0},
            },
        }
        raw_rows = [
            {
                "t": f"2026-06-25T{hour:02d}:00:00Z",
                "o": 100 + index,
                "h": 101 + index,
                "l": 99 + index,
                "c": 100.5 + index,
                "v": 1000 + index,
                "n": 10 + index,
                "vw": 100.25 + index,
            }
            for index, hour in enumerate(range(13, 21))
        ]
        with mock.patch.dict(os.environ, {"ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED": "true"}):
            service = OnDemandFillService(timeout_seconds=8, background_enabled=False)

        with mock.patch("app.market_data.fill.service.fetch_alpaca_bars", return_value=raw_rows) as fetch:
            result = service.fill_if_needed(
                symbol="BAC",
                interval="1h",
                limit=8,
                before=None,
                from_time="2026-06-25T13:00:00.000Z",
                to_time="2026-06-25T20:00:00.000Z",
                payload=payload,
            )

        self.assertEqual(fetch.call_args.args[4], "1Hour")
        self.assertEqual(result["sourceInterval"], "1h")
        self.assertEqual(result["fill"]["sourceInterval"], "1h")
        self.assertEqual(result["fill"]["status"], "filled")
        self.assertEqual(result["fill"]["foregroundFill"]["state"], "filled")
        self.assertTrue(result["fill"]["sources"]["alpaca"]["hit"])
        self.assertEqual(len(result["candles"]), 8)
        self.assertEqual(result["candles"][0]["timestamp"], "2026-06-25T13:00:00.000Z")
        self.assertIn(":1h:2026-06-25T20:00:00.000Z:", result["snapshotCursor"])
        self.assertTrue(result["fill"]["renderable"])

    def test_on_demand_fill_auto_foreground_repairs_daily_sparse_window(self):
        payload = {
            "symbol": "NVDA",
            "interval": "1D",
            "candles": [],
            "returnedCount": 0,
            "storedCandleCount": 0,
            "sourceInterval": "1D",
            "missingRanges": [
                {"start": "2026-01-01T00:00:00.000Z", "end": "2026-03-01T00:00:00.000Z"}
            ],
            "_sourceTrace": {
                "redis": {"checked": True, "hit": False, "rowCount": 0},
                "clickhouse": {"checked": True, "hit": False, "rowCount": 0},
            },
        }
        raw_rows = [
            {
                "t": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)).strftime("%Y-%m-%dT00:00:00Z"),
                "o": 180 + index,
                "h": 181 + index,
                "l": 179 + index,
                "c": 180.5 + index,
                "v": 10_000_000 + index,
                "n": 100 + index,
                "vw": 180.25 + index,
            }
            for index in range(60)
        ]
        with mock.patch.dict(os.environ, {
            "ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED": "",
            "ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS": "1D",
            "ON_DEMAND_FILL_FOREGROUND_AUTO_MAX_BARS": "100",
        }, clear=False):
            service = OnDemandFillService(timeout_seconds=8, background_enabled=False)

        with mock.patch("app.market_data.fill.service.fetch_alpaca_bars", return_value=raw_rows) as fetch:
            result = service.fill_if_needed(
                symbol="NVDA",
                interval="1D",
                limit=60,
                before=None,
                from_time="2026-01-01T00:00:00.000Z",
                to_time="2026-03-01T00:00:00.000Z",
                payload=payload,
            )

        self.assertEqual(fetch.call_args.args[4], "1Day")
        self.assertEqual(result["fill"]["foregroundFill"]["state"], "filled")
        self.assertEqual(result["fill"]["status"], "filled")
        self.assertEqual(result["sourceInterval"], "1D")
        self.assertEqual(len(result["candles"]), 60)
        self.assertEqual(result["candles"][0]["timestamp"], "2026-01-01T00:00:00.000Z")

    def test_on_demand_fill_keeps_live_candle_over_alpaca_current_bucket(self):
        live = {
            "symbol": "BAC",
            "timeframe": "1h",
            "timestamp": "2026-06-25T20:00:00.000Z",
            "open": 200,
            "high": 201,
            "low": 199,
            "close": 200.75,
            "volume": 2000,
            "isClosed": False,
            "sourceInterval": "trades",
        }
        payload = {
            "symbol": "BAC",
            "interval": "1h",
            "candles": [live],
            "returnedCount": 1,
            "storedCandleCount": 1,
        }
        raw_rows = [
            {
                "t": f"2026-06-25T{hour:02d}:00:00Z",
                "o": 100 + index,
                "h": 101 + index,
                "l": 99 + index,
                "c": 100.5 + index,
                "v": 1000 + index,
                "n": 10 + index,
                "vw": 100.25 + index,
            }
            for index, hour in enumerate(range(13, 21))
        ]
        with mock.patch.dict(os.environ, {"ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED": "true"}):
            service = OnDemandFillService(timeout_seconds=8, background_enabled=False)

        with mock.patch("app.market_data.fill.service.fetch_alpaca_bars", return_value=raw_rows):
            result = service.fill_if_needed(
                symbol="BAC",
                interval="1h",
                limit=8,
                before=None,
                from_time="2026-06-25T13:00:00.000Z",
                to_time="2026-06-25T20:00:00.000Z",
                payload=payload,
            )

        self.assertEqual(len(result["candles"]), 8)
        self.assertEqual(result["candles"][-1]["timestamp"], "2026-06-25T20:00:00.000Z")
        self.assertEqual(result["candles"][-1]["close"], 200.75)
        self.assertFalse(result["candles"][-1]["isClosed"])

    def test_background_fill_materializes_s3_before_alpaca(self):
        service = RecordingOnDemandFillService(s3_result=True, alpaca_result=True)

        service._run_background_fill(
            "test-fill",
            "CSCO",
            "1m",
            "1m",
            30,
            "2026-06-25T14:00:00.000Z",
            None,
            None,
            "2026-06-25T13:00:00.000Z",
            "2026-06-25T14:00:00.000Z",
            [{"start": "2026-06-25T13:00:00.000Z", "end": "2026-06-25T14:00:00.000Z"}],
        )

        self.assertEqual([call[0] for call in service.calls], ["s3"])

    def test_background_fill_falls_back_to_alpaca_when_s3_misses(self):
        service = RecordingOnDemandFillService(s3_result=False, alpaca_result=True)

        service._run_background_fill(
            "test-fill",
            "CSCO",
            "1m",
            "1m",
            30,
            "2026-06-25T14:00:00.000Z",
            None,
            None,
            "2026-06-25T13:00:00.000Z",
            "2026-06-25T14:00:00.000Z",
            [{"start": "2026-06-25T13:00:00.000Z", "end": "2026-06-25T14:00:00.000Z"}],
        )

        self.assertEqual([call[0] for call in service.calls], ["s3", "alpaca"])

    def test_background_fill_allows_alpaca_completion_before_deadline_recheck(self):
        service = DeadlineAfterAlpacaFillService()

        service._run_background_fill(
            "test-fill",
            "CSCO",
            "1m",
            "1m",
            30,
            "2026-06-25T14:00:00.000Z",
            None,
            None,
            "2026-06-25T13:00:00.000Z",
            "2026-06-25T14:00:00.000Z",
            [{"start": "2026-06-25T13:00:00.000Z", "end": "2026-06-25T14:00:00.000Z"}],
        )

        self.assertEqual([call[0] for call in service.calls], ["s3", "alpaca"])

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

    def test_backfill_routes_return_gone(self):
        previous = chart_routes.get_query_service
        chart_routes.get_query_service = lambda: FakeQueryService(EmptyFakeProvider())
        try:
            with self.assertRaises(HTTPException) as requested:
                chart_routes.chart_backfill(chart_routes.BackfillRequestBody(
                    symbol="intc",
                    interval="1m",
                    start="2026-06-25T13:30:00.000Z",
                    end="2026-06-25T14:30:00.000Z",
                ))
            with self.assertRaises(HTTPException) as status:
                chart_routes.chart_backfill_status("intc", "1m")
            with self.assertRaises(HTTPException) as queue:
                chart_routes.chart_backfill_queue()
        finally:
            chart_routes.get_query_service = previous

        self.assertEqual(requested.exception.status_code, 410)
        self.assertEqual(status.exception.status_code, 410)
        self.assertEqual(queue.exception.status_code, 410)

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

    def test_active_chart_heartbeat_writes_source_without_websocket(self):
        fake_redis = FakeMonitorRedis()
        fake_provider = types.SimpleNamespace(redis_provider=types.SimpleNamespace(redis=fake_redis))
        previous_provider = chart_routes.get_market_data_provider
        chart_routes.get_market_data_provider = lambda: fake_provider
        try:
            payload = chart_routes.chart_active_symbol_heartbeat(
                chart_routes.ActiveChartHeartbeatBody(symbol="mlm", sessionId="panel-1", ttlSeconds=45),
                user=None,
            )
        finally:
            chart_routes.get_market_data_provider = previous_provider

        from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService

        self.assertEqual(payload["symbol"], "MLM")
        self.assertTrue(payload["pendingReconcile"])
        self.assertIn("anonymous", fake_redis.sets["gops:market:on-demand:v1:subscription:users:active-chart"])
        self.assertEqual(
            fake_redis.hashes["gops:market:on-demand:v1:user:anonymous:active-chart:panel-1"]["symbol"],
            "MLM",
        )

        RealtimeSubscriptionCohortService(fake_redis).reconcile()
        self.assertEqual(fake_redis.sets["gops:market:on-demand:v1:subscription:symbols"], {"MLM"})
        self.assertEqual(
            fake_redis.hashes["gops:market:on-demand:v1:subscription:symbol:MLM"]["sources"],
            "active-chart",
        )

    def test_realtime_monitor_reports_symbol_live_age_and_scoped_health(self):
        fake_redis = FakeMonitorRedis()
        keys = __import__("alfaka.common.redis_keys", fromlist=["RedisKeyBuilder"]).RedisKeyBuilder()
        fake_redis.sadd(keys.subscription_symbols(), "MLM")
        fake_redis.sadd(keys.active_symbols(), "MLM")
        fake_redis.hset(keys.subscription_symbol("MLM"), mapping={
            "symbol": "MLM",
            "enabled": "true",
            "layers": "quotes,trades",
            "sources": "active-chart",
            "ttlSeconds": "45",
        })
        fake_redis.hset(keys.live_trade("MLM"), mapping={
            "symbol": "MLM",
            "price": "547.10",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        })
        fake_redis.set(keys.live_quote("MLM"), json.dumps({
            "symbol": "MLM",
            "bidPrice": 547.0,
            "askPrice": 547.2,
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }))
        fake_redis.set(keys.live_candle("MLM", "1m"), json.dumps({
            "symbol": "MLM",
            "interval": "1m",
            "close": 547.1,
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }))
        fake_redis.set(keys.component_health("market-processor:symbol:MLM"), json.dumps({
            "component": "market-processor:symbol:MLM",
            "status": "ok",
            "lastSymbol": "MLM",
            "lastFeedProfile": "sip",
        }))
        service_mod = __import__("app.market_data.monitor.service", fromlist=["MarketDataMonitorService"])
        service = service_mod.MarketDataMonitorService(redis_client=fake_redis)
        with mock.patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": ""}):
            payload = service.realtime(symbol="MLM", interval="1m")

        self.assertEqual(payload["subscriptionVersion"], "0")
        self.assertEqual(payload["symbol"]["symbol"], "MLM")
        self.assertTrue(payload["symbol"]["subscribed"])
        self.assertTrue(payload["symbol"]["liveTradePresent"])
        self.assertTrue(payload["symbol"]["liveQuotePresent"])
        self.assertTrue(payload["symbol"]["liveCandlePresent"])
        self.assertIsNotNone(payload["symbol"]["liveCandleAgeSeconds"])
        self.assertEqual(payload["symbolProcessorHealth"]["lastSymbol"], "MLM")
        self.assertFalse(payload["kafka"]["enabled"])

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

    def test_chart_symbols_route_applies_limit_without_query(self):
        provider = FakeWatchlistProvider()
        previous_provider = market_data_service.get_market_data_provider
        previous_universe = market_data_service.configured_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.configured_universe_symbols = lambda: ["AAPL", "BRK.B", "JPM"]
        try:
            payload = chart_routes.chart_symbols(query=None, limit=1)
        finally:
            market_data_service.get_market_data_provider = previous_provider
            market_data_service.configured_universe_symbols = previous_universe

        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["AAPL"])

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

    def test_symbol_summary_ignores_stale_redis_live_state(self):
        class StaleRedisProvider:
            def __init__(self):
                self.redis = FakeWatchlistRedis()
                keys = RedisKeyBuilder()
                self.redis.set(keys.latest_closed_candle("MLM", "1m"), json.dumps({
                    "symbol": "MLM",
                    "interval": "1m",
                    "timestamp": "2000-01-01T13:15:00.000Z",
                    "close": 602.94,
                }))

            def latest_price(self, symbol):
                return {
                    "symbol": symbol,
                    "price": "602.94",
                    "timestamp": "2000-01-01T13:15:20.000Z",
                }

        class FreshClickHouseProvider:
            def candles(self, symbol, interval, limit):
                if interval != "1D":
                    return []
                return [{
                    "symbol": symbol,
                    "interval": "1D",
                    "timestamp": "2026-07-06T04:00:00.000Z",
                    "close": 599.11,
                }]

        class Provider:
            def __init__(self):
                self.redis_provider = StaleRedisProvider()
                self.clickhouse_provider = FreshClickHouseProvider()

            def symbol_detail(self, symbol):
                return {"symbol": symbol, "name": "Martin Marietta Materials", "market": "NYSE"}

        with mock.patch.dict(os.environ, {
            "SYMBOL_LIVE_PRICE_STALE_SECONDS": "180",
            "SYMBOL_REDIS_INTRADAY_STALE_SECONDS": "300",
        }):
            summary = market_data_service.build_symbol_summary("MLM", provider=Provider())

        self.assertEqual(summary["lastPrice"], 599.11)
        self.assertEqual(summary["priceSource"], "clickhouse")

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

    def test_watchlist_read_preserves_user_order(self):
        provider = FakeWatchlistProvider()
        previous = market_data_service.get_market_data_provider
        previous_universe = market_data_service.configured_universe_symbols
        market_data_service.get_market_data_provider = lambda: provider
        market_data_service.configured_universe_symbols = lambda: ["AAPL", "MSFT", "NVDA"]
        try:
            market_data_service.replace_watchlist_symbols("user-a", ["NVDA", "AAPL", "MSFT"])
            payload = market_data_service.watchlist_summaries(user_id="user-a")
        finally:
            market_data_service.get_market_data_provider = previous
            market_data_service.configured_universe_symbols = previous_universe

        self.assertEqual([item["symbol"] for item in payload["symbols"]], ["NVDA", "AAPL", "MSFT"])

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

    def test_market_heatmap_combines_fundamentals_quotes_seed_and_cache(self):
        seed_items = [
            {
                "symbol": "AAPL",
                "companyName": "Apple Inc.",
                "sector": "Technology",
                "industry": "Technology Hardware",
                "marketCap": 100000,
                "changePercent": 0.1,
            },
            {
                "symbol": "MSFT",
                "companyName": "Microsoft",
                "sector": "Technology",
                "industry": "Systems Software",
                "marketCap": 200000,
                "changePercent": 0.2,
            },
        ]
        provider = FakeHeatmapProvider()
        adapter = FakeFundamentalsAdapter({
            "AAPL": FundamentalsRecord(
                symbol="AAPL",
                companyName="Apple Inc.",
                sector="Technology",
                industry="Technology Hardware",
                sharesOutstanding=1000,
                revenue=100000,
                eps=5,
                totalEquity=50000,
                freeCashFlow=10000,
                source="sec",
                asOf="2026-07-05",
                periodEndDate="2026-04-30",
            )
        })
        with mock.patch.object(heatmap_service, "load_heatmap_seed_items", return_value=seed_items), mock.patch.object(
            heatmap_service,
            "utc_now",
            return_value=datetime(2026, 6, 25, 15, 34, tzinfo=timezone.utc),
        ), mock.patch.dict(os.environ, {
            "HEATMAP_QUOTE_REFRESH_SECONDS": "60",
            "HEATMAP_LAYOUT_REFRESH_SECONDS": "300",
            "HEATMAP_CACHE_TTL_SECONDS": "55",
        }):
            service = heatmap_service.MarketHeatmapService(provider=provider, fundamentals_adapter=adapter)
            payload = service.snapshot("sp500")
            cached_payload = service.snapshot("sp500")

            redis_state = provider.redis_provider.redis
            redis_state.values.pop("gops:market:on-demand:v1:heatmap:sp500", None)
            provider.clickhouse_provider.rows = []
            stale_payload = service.snapshot("sp500")

        self.assertEqual(payload["quoteRefreshSeconds"], 60)
        self.assertEqual(payload["layoutRefreshSeconds"], 300)
        self.assertEqual(payload["layoutAsOf"], "2026-06-25T15:30:00Z")
        self.assertEqual(provider.clickhouse_provider.calls[:1], [{"symbols": ["AAPL", "MSFT"], "limit": 2, "method": "latest_quotes"}])
        self.assertEqual(cached_payload["items"][0]["symbol"], "AAPL")
        self.assertEqual(payload["coverage"]["marketCapFromFundamentals"], 1)
        self.assertEqual(payload["coverage"]["marketCapFromSeed"], 1)
        self.assertEqual(payload["coverage"]["layoutMarketCapFromFundamentals"], 1)
        self.assertEqual(payload["coverage"]["layoutMarketCapFromSeed"], 1)
        self.assertEqual(payload["items"][0]["marketCap"], 200000)
        self.assertEqual(payload["items"][0]["marketCapSource"], "fundamentals")
        self.assertEqual(payload["items"][0]["sector"], "Information Technology")
        self.assertEqual(payload["items"][0]["sectorLabelKo"], "정보기술")
        self.assertEqual(payload["items"][0]["layoutPrice"], 200)
        self.assertEqual(payload["items"][0]["eps"], 5)
        self.assertEqual(payload["items"][0]["revenue"], 100000)
        self.assertEqual(payload["items"][0]["totalEquity"], 50000)
        self.assertEqual(payload["items"][0]["freeCashFlow"], 10000)
        self.assertEqual(payload["items"][0]["layoutMarketCap"], 200000)
        self.assertEqual(payload["items"][0]["layoutMarketCapSource"], "fundamentals")
        self.assertEqual(payload["items"][0]["fundamentalsAsOf"], "2026-07-05")
        self.assertEqual(payload["items"][0]["changePercent"], 1.25)
        self.assertEqual(payload["items"][1]["marketCap"], 200000)
        self.assertEqual(payload["items"][1]["marketCapSource"], "seed")
        self.assertEqual(payload["items"][1]["layoutMarketCap"], 200000)
        self.assertEqual(payload["items"][1]["layoutMarketCapSource"], "seed")
        self.assertEqual(stale_payload["items"][0]["lastPrice"], 200)
        self.assertEqual(stale_payload["items"][0]["priceSource"], "cached-projection")
        self.assertEqual(stale_payload["items"][0]["layoutPrice"], 200)
        self.assertEqual(stale_payload["items"][0]["layoutMarketCap"], 200000)

    def test_market_heatmap_layout_bucket_uses_projection_time_not_stale_quote_time(self):
        seed_items = [{
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Technology Hardware",
            "marketCap": 100000,
            "changePercent": 0.1,
        }]
        provider = FakeHeatmapProvider(rows=[{
            "symbol": "AAPL",
            "lastPrice": 200,
            "changePercent": 1.25,
            "sourceUpdatedAt": "2026-06-25T15:31:00.000Z",
            "rankReason": "clickhouse_1m_session_aggregate",
        }])
        adapter = FakeFundamentalsAdapter({
            "AAPL": FundamentalsRecord(symbol="AAPL", sharesOutstanding=1000, source="sec", asOf="2026-07-05")
        })
        with mock.patch.object(heatmap_service, "load_heatmap_seed_items", return_value=seed_items), mock.patch.object(
            heatmap_service,
            "utc_now",
            return_value=datetime(2026, 6, 25, 15, 36, tzinfo=timezone.utc),
        ), mock.patch.dict(os.environ, {"HEATMAP_LAYOUT_REFRESH_SECONDS": "300"}):
            payload = heatmap_service.MarketHeatmapService(provider=provider, fundamentals_adapter=adapter).snapshot("sp500")

        self.assertEqual(payload["quoteAsOf"], "2026-06-25T15:31:00Z")
        self.assertEqual(payload["layoutAsOf"], "2026-06-25T15:35:00Z")
        self.assertEqual(payload["items"][0]["layoutMarketCap"], 200000)

    def test_market_indices_fetches_yahoo_once_and_reuses_fresh_cache(self):
        provider = FakeHeatmapProvider()
        calls = []

        def fetcher(**kwargs):
            calls.append(kwargs)
            return {
                "^GSPC": [
                    {
                        "timestamp": "2026-07-02T20:00:00Z",
                        "Open": 99,
                        "High": 101,
                        "Low": 98,
                        "Close": 100,
                        "Volume": 1000,
                    },
                    {
                        "timestamp": "2026-07-03T20:00:00Z",
                        "Open": 100,
                        "High": 102,
                        "Low": 99,
                        "Close": 101,
                        "Volume": 1200,
                    },
                ],
                "KRW=X": [
                    {"timestamp": "2026-07-02T20:00:00Z", "Close": 1300},
                    {"timestamp": "2026-07-03T20:00:00Z", "Close": 1305},
                ],
            }

        with mock.patch.object(indices_service, "utc_now", return_value=datetime(2026, 7, 3, 20, 1, tzinfo=timezone.utc)), mock.patch.dict(
            os.environ,
            {
                "MARKET_INDICES_CACHE_TTL_SECONDS": "30",
                "MARKET_INDICES_REFRESH_SECONDS": "30",
                "MARKET_INDICES_STALE_REFRESH_SECONDS": "300",
            },
        ):
            service = indices_service.MarketIndicesService(provider=provider, fetcher=fetcher)
            payload = service.snapshot()
            cached_payload = service.snapshot()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["period"], "5d")
        self.assertEqual(calls[0]["interval"], "5m")
        self.assertIn("^GSPC", calls[0]["symbols"])
        self.assertEqual(payload["cacheStatus"], "fresh")
        self.assertEqual(payload["updatedAt"], "2026-07-03T20:01:00Z")
        self.assertEqual(payload["coverage"]["total"], len(indices_service.INDEX_DEFINITIONS))
        self.assertEqual(payload["coverage"]["priced"], 2)
        self.assertEqual(cached_payload["cacheStatus"], "fresh")
        sp500 = payload["items"][0]
        self.assertEqual(sp500["symbol"], "^GSPC")
        self.assertEqual(sp500["price"], 101)
        self.assertEqual(sp500["previousClose"], 100)
        self.assertEqual(sp500["change"], 1)
        self.assertEqual(sp500["changePercent"], 1)
        self.assertEqual(sp500["sparkline"], [100, 101])
        self.assertEqual(provider.redis_provider.redis.expirations[indices_service.indices_cache_key()], 30)

    def test_market_indices_returns_stale_immediately_and_refreshes_in_background(self):
        provider = FakeHeatmapProvider()
        calls = []

        def fetcher(**kwargs):
            calls.append(kwargs)
            latest_close = 100 + len(calls)
            return {
                "^GSPC": [
                    {"timestamp": "2026-07-02T20:00:00Z", "Close": 100},
                    {"timestamp": "2026-07-03T20:00:00Z", "Close": latest_close},
                ],
            }

        class FakeBackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, func, *args, **kwargs):
                self.tasks.append((func, args, kwargs))

        service = indices_service.MarketIndicesService(provider=provider, fetcher=fetcher)
        first_payload = service.snapshot()
        provider.redis_provider.redis.values.pop(indices_service.indices_cache_key(), None)
        background_tasks = FakeBackgroundTasks()

        stale_payload = service.snapshot(background_tasks=background_tasks)
        task, args, kwargs = background_tasks.tasks[0]
        task(*args, **kwargs)
        refreshed_payload = service.snapshot()

        self.assertEqual(len(calls), 2)
        self.assertEqual(first_payload["items"][0]["price"], 101)
        self.assertEqual(stale_payload["cacheStatus"], "stale")
        self.assertIn("Refreshing in background", stale_payload["warning"])
        self.assertEqual(stale_payload["items"][0]["price"], 101)
        self.assertEqual(refreshed_payload["cacheStatus"], "fresh")
        self.assertEqual(refreshed_payload["items"][0]["price"], 102)
        self.assertNotIn(indices_service.indices_lock_key(), provider.redis_provider.redis.values)

    def test_market_indices_returns_stale_cache_when_refresh_locked_or_yahoo_fails(self):
        provider = FakeHeatmapProvider()
        redis_state = provider.redis_provider.redis

        def fetcher(**_kwargs):
            return {
                "^GSPC": [
                    {"timestamp": "2026-07-02T20:00:00Z", "Close": 100},
                    {"timestamp": "2026-07-03T20:00:00Z", "Close": 101},
                ],
            }

        service = indices_service.MarketIndicesService(provider=provider, fetcher=fetcher)
        service.snapshot()
        redis_state.values.pop(indices_service.indices_cache_key(), None)
        redis_state.set(indices_service.indices_lock_key(), "locked", ex=15)

        locked_payload = service.snapshot()

        def failing_fetcher(**_kwargs):
            raise TimeoutError("upstream timeout")

        redis_state.delete(indices_service.indices_lock_key())
        failing_service = indices_service.MarketIndicesService(provider=provider, fetcher=failing_fetcher)
        failed_payload = failing_service.snapshot()

        self.assertEqual(locked_payload["cacheStatus"], "stale")
        self.assertIn("Refresh already in progress", locked_payload["warning"])
        self.assertEqual(locked_payload["refreshSeconds"], indices_service.DEFAULT_STALE_REFRESH_SECONDS)
        self.assertEqual(failed_payload["cacheStatus"], "stale")
        self.assertIn("Yahoo Finance refresh failed", failed_payload["warning"])

    def test_market_indices_refresh_forces_cache_warm_without_panel_request(self):
        provider = FakeHeatmapProvider()
        calls = []

        def fetcher(**kwargs):
            calls.append(kwargs)
            latest_close = 100 + len(calls)
            return {
                "^GSPC": [
                    {"timestamp": "2026-07-02T20:00:00Z", "Close": 100},
                    {"timestamp": "2026-07-03T20:00:00Z", "Close": latest_close},
                ],
            }

        service = indices_service.MarketIndicesService(provider=provider, fetcher=fetcher)
        first_payload = service.snapshot()
        warmed_payload = service.refresh()
        cached_payload = service.snapshot()

        self.assertEqual(len(calls), 2)
        self.assertEqual(first_payload["items"][0]["price"], 101)
        self.assertEqual(warmed_payload["items"][0]["price"], 102)
        self.assertEqual(cached_payload["items"][0]["price"], 102)
        self.assertEqual(provider.redis_provider.redis.expirations[indices_service.indices_cache_key()], indices_service.DEFAULT_CACHE_TTL_SECONDS)

    def test_market_heatmap_keeps_layout_cap_stable_within_layout_bucket(self):
        seed_items = [{
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Technology Hardware",
            "marketCap": 100000,
            "changePercent": 0.1,
        }]
        provider = FakeHeatmapProvider(rows=[{
            "symbol": "AAPL",
            "lastPrice": 200,
            "changePercent": 1.25,
            "sourceUpdatedAt": "2026-06-25T15:31:00.000Z",
            "rankReason": "clickhouse_1m_session_aggregate",
        }])
        adapter = FakeFundamentalsAdapter({
            "AAPL": FundamentalsRecord(symbol="AAPL", sharesOutstanding=1000, source="sec", asOf="2026-07-05")
        })
        with mock.patch.object(heatmap_service, "load_heatmap_seed_items", return_value=seed_items), mock.patch.object(
            heatmap_service,
            "utc_now",
            return_value=datetime(2026, 6, 25, 15, 34, tzinfo=timezone.utc),
        ), mock.patch.dict(os.environ, {"HEATMAP_LAYOUT_REFRESH_SECONDS": "300"}):
            service = heatmap_service.MarketHeatmapService(provider=provider, fundamentals_adapter=adapter)
            first_payload = service.snapshot("sp500")
            provider.redis_provider.redis.values.pop("gops:market:on-demand:v1:heatmap:sp500", None)
            provider.clickhouse_provider.rows = [{
                "symbol": "AAPL",
                "lastPrice": 210,
                "changePercent": 2.0,
                "sourceUpdatedAt": "2026-06-25T15:32:00.000Z",
                "rankReason": "clickhouse_1m_session_aggregate",
            }]
            second_payload = service.snapshot("sp500")

        self.assertEqual(first_payload["layoutAsOf"], "2026-06-25T15:30:00Z")
        self.assertEqual(second_payload["layoutAsOf"], "2026-06-25T15:30:00Z")
        self.assertEqual(second_payload["items"][0]["lastPrice"], 210)
        self.assertEqual(second_payload["items"][0]["marketCap"], 210000)
        self.assertEqual(second_payload["items"][0]["layoutPrice"], 200)
        self.assertEqual(second_payload["items"][0]["layoutMarketCap"], 200000)

    def test_market_heatmap_upgrades_seed_layout_when_fundamentals_projection_arrives(self):
        seed_items = [{
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Technology Hardware",
            "marketCap": 100000,
            "changePercent": 0.1,
        }]
        provider = FakeHeatmapProvider(rows=[])
        adapter = FakeFundamentalsAdapter({
            "AAPL": FundamentalsRecord(symbol="AAPL", sharesOutstanding=1000, source="sec", asOf="2026-07-05")
        })
        with mock.patch.object(heatmap_service, "load_heatmap_seed_items", return_value=seed_items), mock.patch.object(
            heatmap_service,
            "utc_now",
            return_value=datetime(2026, 6, 25, 15, 34, tzinfo=timezone.utc),
        ), mock.patch.dict(os.environ, {"HEATMAP_LAYOUT_REFRESH_SECONDS": "300"}):
            service = heatmap_service.MarketHeatmapService(provider=provider, fundamentals_adapter=adapter)
            first_payload = service.snapshot("sp500")
            provider.redis_provider.redis.values.pop("gops:market:on-demand:v1:heatmap:sp500", None)
            provider.clickhouse_provider.rows = [{
                "symbol": "AAPL",
                "lastPrice": 210,
                "changePercent": 2.0,
                "sourceUpdatedAt": "2026-06-25T15:32:00.000Z",
                "rankReason": "clickhouse_1m_latest_quote",
            }]
            second_payload = service.snapshot("sp500")

        self.assertEqual(first_payload["items"][0]["layoutMarketCapSource"], "seed")
        self.assertEqual(second_payload["layoutAsOf"], "2026-06-25T15:30:00Z")
        self.assertEqual(second_payload["items"][0]["marketCapSource"], "fundamentals")
        self.assertEqual(second_payload["items"][0]["layoutPrice"], 210)
        self.assertEqual(second_payload["items"][0]["layoutMarketCap"], 210000)
        self.assertEqual(second_payload["items"][0]["layoutMarketCapSource"], "fundamentals")

    def test_market_heatmap_overlays_redis_live_price_on_clickhouse_rows(self):
        seed_items = [{
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Technology Hardware",
            "marketCap": 100000,
            "changePercent": 0.1,
        }]
        provider = FakeHeatmapProvider(redis_prices={
            "AAPL": {"price": "201.5", "timestamp": "2026-06-25T15:32:00.000Z"}
        })
        adapter = FakeFundamentalsAdapter({
            "AAPL": FundamentalsRecord(symbol="AAPL", sharesOutstanding=1000, source="sec", asOf="2026-07-05")
        })
        with mock.patch.object(heatmap_service, "load_heatmap_seed_items", return_value=seed_items):
            payload = heatmap_service.MarketHeatmapService(provider=provider, fundamentals_adapter=adapter).snapshot("sp500")

        self.assertEqual(payload["items"][0]["lastPrice"], 201.5)
        self.assertEqual(payload["items"][0]["marketCap"], 201500)
        self.assertEqual(payload["items"][0]["layoutPrice"], 201.5)
        self.assertEqual(payload["items"][0]["layoutMarketCap"], 201500)
        self.assertEqual(payload["items"][0]["priceSource"], "redis_live")

    def test_store_fundamentals_adapter_reads_redis_summary_before_clickhouse(self):
        provider = FakeHeatmapProvider()
        provider.redis_provider.redis.values["gops:fundamentals:summary:v1:AAPL"] = json.dumps({
            "symbol": "AAPL",
            "cik": "0000320193",
            "source": "sec_companyfacts",
            "source_filed_at": "2026-05-01",
            "as_of": "2026-04-30",
            "metrics": [{
                "metric": "shares_outstanding",
                "value": 1000,
                "fiscalPeriod": "Q2",
                "periodEnd": "2026-04-30",
                "filedAt": "2026-05-01",
                "unit": "shares",
            }, {
                "metric": "eps",
                "value": 3.5,
                "fiscalPeriod": "Q2",
                "periodEnd": "2026-04-30",
                "filedAt": "2026-05-01",
            }, {
                "metric": "revenue",
                "value": 90000,
                "fiscalPeriod": "Q2",
                "periodEnd": "2026-04-30",
                "filedAt": "2026-05-01",
            }, {
                "metric": "equity",
                "value": 45000,
                "fiscalPeriod": "Q2",
                "periodEnd": "2026-04-30",
                "filedAt": "2026-05-01",
            }, {
                "metric": "free_cash_flow",
                "value": 12000,
                "fiscalPeriod": "Q2",
                "periodEnd": "2026-04-30",
                "filedAt": "2026-05-01",
            }],
        })

        records = StoreFundamentalsAdapter(provider=provider).latest_for_symbols(["AAPL"])

        self.assertEqual(records["AAPL"].sharesOutstanding, 1000)
        self.assertEqual(records["AAPL"].eps, 3.5)
        self.assertEqual(records["AAPL"].revenue, 90000)
        self.assertEqual(records["AAPL"].totalEquity, 45000)
        self.assertEqual(records["AAPL"].freeCashFlow, 12000)
        self.assertEqual(records["AAPL"].cik, "0000320193")
        self.assertEqual(records["AAPL"].source, "sec_companyfacts:redis")
        self.assertEqual(records["AAPL"].asOf, "2026-04-30")

    def test_store_fundamentals_adapter_falls_back_to_clickhouse_tables(self):
        records = StoreFundamentalsAdapter(provider=FakeHeatmapProvider()).latest_for_symbols(["MSFT"])

        self.assertEqual(records["MSFT"].companyName, "Microsoft Corporation")
        self.assertEqual(records["MSFT"].sharesOutstanding, 7500)
        self.assertEqual(records["MSFT"].eps, 4)
        self.assertEqual(records["MSFT"].revenue, 100000)
        self.assertEqual(records["MSFT"].totalEquity, 50000)
        self.assertEqual(records["MSFT"].freeCashFlow, 25000)
        self.assertEqual(records["MSFT"].source, "sec_companyfacts")
        self.assertEqual(records["MSFT"].periodEndDate, "2026-03-31")

    def test_store_fundamentals_adapter_supplements_incomplete_redis_summary_from_clickhouse(self):
        provider = FakeHeatmapProvider()
        provider.redis_provider.redis.values["gops:fundamentals:summary:v1:MSFT"] = json.dumps({
            "symbol": "MSFT",
            "source": "sec_companyfacts",
            "as_of": "2026-03-31",
            "metrics": [{
                "metric": "revenue",
                "value": 100000,
                "fiscalPeriod": "Q1",
                "periodEnd": "2026-03-31",
                "filedAt": "2026-04-25",
            }],
        })

        records = StoreFundamentalsAdapter(provider=provider).latest_for_symbols(["MSFT"])

        self.assertEqual(records["MSFT"].revenue, 100000)
        self.assertEqual(records["MSFT"].sharesOutstanding, 7500)
        self.assertEqual(records["MSFT"].eps, 4)
        self.assertEqual(records["MSFT"].totalEquity, 50000)
        self.assertIn("sec_companyfacts", records["MSFT"].source)

    def test_store_fundamentals_adapter_returns_sec_financial_series(self):
        series = StoreFundamentalsAdapter(provider=FakeHeatmapProvider()).financial_series("MSFT")

        self.assertEqual(series[0].period, "2026Q1")
        self.assertEqual(series[0].periodEndDate, "2026-03-31")
        self.assertEqual(series[0].revenue, 100000)
        self.assertEqual(series[0].netIncome, None)
        self.assertEqual(series[0].eps, 4)
        self.assertEqual(series[0].totalEquity, 50000)
        self.assertEqual(series[0].freeCashFlow, 25000)
        self.assertEqual(series[0].source, "sec")

    def test_store_fundamentals_adapter_returns_earnings_series_with_yahoo_estimates(self):
        series = StoreFundamentalsAdapter(provider=FakeHeatmapProvider()).earnings_series("MSFT")

        self.assertEqual(series[0].period, "2026Q1")
        self.assertEqual(series[0].actualEps, 4)
        self.assertEqual(series[0].estimatedEps, 4.5)
        self.assertEqual(series[0].actualRevenue, 100000)
        self.assertEqual(series[0].estimatedRevenue, 95000)
        self.assertEqual(series[0].estimateSource, "yahoo")

    def test_query_service_returns_sec_financial_series_payload(self):
        payload = MarketDataQueryService(provider=FakeHeatmapProvider()).financial_series("MSFT", years=3, period="quarterly")

        self.assertEqual(payload["source"], "sec")
        self.assertEqual(payload["symbol"], "MSFT")
        self.assertEqual(payload["period"], "quarterly")
        self.assertEqual(payload["items"][0]["period"], "2026Q1")
        self.assertEqual(payload["items"][0]["revenue"], 100000)
        self.assertEqual(payload["items"][0]["freeCashFlow"], 25000)

    def test_query_service_returns_earnings_series_payload(self):
        payload = MarketDataQueryService(provider=FakeHeatmapProvider()).earnings_series("MSFT", years=3)

        self.assertEqual(payload["source"], "sec-yahoo")
        self.assertEqual(payload["symbol"], "MSFT")
        self.assertEqual(payload["items"][0]["actualEps"], 4)
        self.assertEqual(payload["items"][0]["estimatedEps"], 4.5)

    def test_fundamentals_adapter_accepts_symbol_keyed_latest_payload(self):
        records = records_from_payload({
            "NVDA": {
                "companyName": "NVIDIA Corporation",
                "sector": "Technology",
                "industry": "Semiconductors",
                "sharesOutstanding": 24000000000,
                "source": "sec",
                "asOf": "2026-07-05",
            }
        }, symbols=["nvda"])

        self.assertEqual(records["NVDA"].companyName, "NVIDIA Corporation")
        self.assertEqual(records["NVDA"].sharesOutstanding, 24000000000)
        self.assertEqual(records["NVDA"].source, "sec")

    def test_sp500_symbol_page_does_not_queue_latest_daily_backfill_when_price_missing(self):
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

        self.assertEqual(backfill.calls, [])
        self.assertEqual(payload["symbols"][0]["lastPrice"], None)
        self.assertEqual(payload["symbols"][0]["priceSource"], None)
        self.assertEqual(payload["symbols"][0]["priceStatus"], "missing")
        self.assertNotIn("latestPriceBackfill", payload["symbols"][0])

    def test_backfill_request_service_returns_gone(self):
        service = BackfillService(store=RecordingBackfillStore())

        with self.assertRaises(HTTPException) as raised:
            service.request_backfill(
                "AAPL",
                "1m",
                start="2020-07-01T00:00:00.000Z",
                end="2026-06-30T00:00:00.000Z",
                force=True,
            )

        self.assertEqual(raised.exception.status_code, 410)
        self.assertIn("on-demand fill", str(raised.exception.detail))

    def test_backfill_status_and_queue_services_return_gone(self):
        service = BackfillService(store=RecordingBackfillStore())

        with self.assertRaises(HTTPException) as status:
            service.get_status("AAPL", "1M")
        with self.assertRaises(HTTPException) as queue:
            service.queue_metrics()

        self.assertEqual(status.exception.status_code, 410)
        self.assertEqual(queue.exception.status_code, 410)

    def test_derived_interval_snapshot_metadata_uses_source_interval(self):
        service = BackfillService(store=RecordingBackfillStore())

        metadata = service.snapshot_metadata("AAPL", "1W", {
            "candles": [],
            "returnedCount": 0,
            "requestedLimit": 260,
            "storedCandleCount": 0,
            "targetStoredCount": 1512,
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["reasonCode"], "no_stored_candles")
        self.assertEqual(metadata["coverage"]["repairStatus"], "gapfill_required")

    def test_intraday_derived_snapshot_metadata_uses_1m_source_interval(self):
        service = BackfillService(store=RecordingBackfillStore())

        metadata = service.snapshot_metadata("AAPL", "1h", {
            "candles": [],
            "returnedCount": 0,
            "requestedLimit": 120,
            "storedCandleCount": 0,
            "targetStoredCount": 7200,
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["sourceInterval"], "1m")
        self.assertEqual(metadata["coverage"]["sourceInterval"], "1m")
        self.assertEqual(metadata["coverage"]["minimumReturnedCount"], 8)

    def test_snapshot_metadata_respects_direct_alpaca_source_interval(self):
        service = BackfillService(store=RecordingBackfillStore())
        candles = [
            {"timestamp": f"2026-06-25T{13 + index:02d}:00:00.000Z"}
            for index in range(8)
        ]

        metadata = service.snapshot_metadata("AAPL", "1h", {
            "candles": candles,
            "returnedCount": 8,
            "requestedLimit": 8,
            "storedCandleCount": 8,
            "targetStoredCount": 8,
            "sourceInterval": "1h",
            "availableFrom": candles[0]["timestamp"],
            "availableTo": candles[-1]["timestamp"],
            "targetRangeFrom": candles[0]["timestamp"],
            "targetRangeTo": candles[-1]["timestamp"],
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["sourceInterval"], "1h")
        self.assertEqual(metadata["coverage"]["sourceInterval"], "1h")
        self.assertEqual(metadata["coverage"]["minimumRenderableSourceBars"], 8)

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
        self.assertNotIn("backfillStatus", partial)
        self.assertNotIn("canBackfill", partial)
        self.assertEqual(partial["coverage"]["reasonCode"], "insufficient_source_bars")
        self.assertEqual(partial["coverage"]["repairStatus"], "gapfill_required")
        self.assertFalse(partial["coverage"]["renderable"])
        self.assertEqual(empty["dataStatus"], "empty")
        self.assertEqual(empty["coverage"]["repairStatus"], "gapfill_required")
        self.assertEqual(empty["coverage"]["reasonCode"], "no_stored_candles")
        self.assertIn("No stored", empty["message"])

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
        self.assertNotIn("backfillStatus", metadata)
        self.assertNotIn("canBackfill", metadata)
        self.assertEqual(metadata["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["reasonCode"], "no_stored_candles")
        self.assertEqual(metadata["coverage"]["repairStatus"], "gapfill_required")

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
        self.assertEqual(metadata["coverage"]["repairStatus"], "gapfill_required")
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
        self.assertEqual(metadata["coverage"]["repairStatus"], "gapfill_required")
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
        self.assertEqual(metadata["coverage"]["repairStatus"], "gapfill_required")
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
        self.assertEqual(metadata["coverage"]["repairStatus"], "none")
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
        self.assertEqual(metadata["coverage"]["repairStatus"], "none")
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

    def test_next_market_open_skips_us_equity_holiday(self):
        with mock.patch.dict(os.environ, {
            "MARKET_TIMEZONE": "America/New_York",
            "MARKET_OPEN_TIME": "09:30",
            "MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS": "true",
            "MARKET_CLOSED_DATES": "",
            "MARKET_EARLY_CLOSES": "",
        }, clear=False):
            payload = next_market_open_payload(
                datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc),
                clock_provider=lambda: None,
            )

        self.assertEqual(payload["source"], "configured-nyse")
        self.assertEqual(payload["marketDate"], "2026-07-06")
        self.assertEqual(payload["nextOpenAt"], "2026-07-06T13:30:00+00:00")
        self.assertIn("2026-07-03", us_equity_holidays(2026))

    def test_next_market_open_prefers_alpaca_clock_payload(self):
        payload = next_market_open_payload(
            datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc),
            clock_provider=lambda: {
                "is_open": False,
                "next_open": "2026-07-07T13:30:00Z",
                "next_close": "2026-07-07T20:00:00Z",
            },
        )

        self.assertEqual(payload["source"], "alpaca-clock")
        self.assertEqual(payload["marketDate"], "2026-07-07")
        self.assertEqual(payload["nextOpenAt"], "2026-07-07T13:30:00+00:00")

    @unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient dependency is not installed")
    def test_fastapi_next_market_open_route_is_public(self):
        from app.main import create_app

        app = create_app()
        app.state.market_clock_provider = lambda: {
            "is_open": False,
            "next_open": "2099-07-07T13:30:00Z",
        }
        response = TestClient(app).get("/api/market/next-open")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["nextOpenAt"], "2099-07-07T13:30:00+00:00")

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
            watchlist_read = client.get("/api/charts/watchlist")
            watchlist_write = client.put("/api/charts/watchlist", json={"symbols": ["NVDA"]})
            portfolio = client.put("/api/charts/subscription-cohorts/portfolio", json={"symbols": ["AAPL"]})
            backfill = client.post("/api/charts/backfill", json={"symbol": "NVDA", "interval": "1m"})

        self.assertEqual(watchlist_read.status_code, 401)
        self.assertEqual(watchlist_write.status_code, 401)
        self.assertEqual(portfolio.status_code, 401)
        self.assertEqual(backfill.status_code, 410)

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
        self.assertEqual(payload["redisCandleCacheLimit"], {
            "1m": 120,
            "5m": 120,
            "10m": 120,
            "1h": 120,
            "4h": 120,
            "1D": 120,
            "1W": 104,
            "1M": 36,
        })

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
        self.assertIn("invalid_alpaca_credential_source", payload["warnings"])
        self.assertNotIn("semiconductor-100", str(payload))


class ChartCompareServiceTest(unittest.TestCase):
    def setUp(self):
        self.provider = FakeCompareProvider()
        self.calls = []

    def make_service(self, bars_by_symbol):
        def fetcher(symbol, start, end, feed, timeframe):
            self.calls.append({"symbol": symbol, "start": start, "end": end, "feed": feed, "timeframe": timeframe})
            value = bars_by_symbol.get(symbol)
            if isinstance(value, Exception):
                raise value
            return value or []

        return ChartCompareService(
            provider=self.provider,
            fetcher=fetcher,
            now=lambda: datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc),
        )

    def test_compare_1d_uses_minute_bars_and_first_close_return(self):
        service = self.make_service({
            "NVDA": [
                {"t": "2026-07-03T14:30:00Z", "c": 90},
                {"t": "2026-07-06T13:30:00Z", "c": 100},
                {"t": "2026-07-06T14:30:00Z", "c": 110},
            ],
            "AMD": [
                {"t": "2026-07-06T13:30:00Z", "c": 50},
                {"t": "2026-07-06T14:30:00Z", "c": 55},
            ],
        })

        payload = service.snapshot(["NVDA", "AMD"], "1D")

        self.assertEqual({call["timeframe"] for call in self.calls}, {"1Min"})
        nvda = next(item for item in payload["items"] if item["symbol"] == "NVDA")
        self.assertEqual([point["time"] for point in nvda["points"]], ["2026-07-06T13:30:00Z", "2026-07-06T14:30:00Z"])
        self.assertEqual(nvda["basePrice"], 100)
        self.assertEqual(nvda["lastPrice"], 110)
        self.assertAlmostEqual(nvda["changePercent"], 10.0)
        self.assertAlmostEqual(nvda["points"][-1]["returnPercent"], 10.0)

    def test_compare_range_timeframe_mapping(self):
        for range_value, expected_timeframe in {
            "1M": "1Hour",
            "6M": "1Day",
            "1Y": "1Day",
            "5Y": "1Week",
        }.items():
            self.calls = []
            service = self.make_service({"NVDA": [{"t": "2026-07-06T14:30:00Z", "c": 100}, {"t": "2026-07-06T15:30:00Z", "c": 101}]})
            payload = service.snapshot(["NVDA"], range_value)
            self.assertEqual(payload["timeframe"], expected_timeframe)
            self.assertEqual(self.calls[0]["timeframe"], expected_timeframe)

    def test_compare_returns_partial_payload_when_one_symbol_fails(self):
        service = self.make_service({
            "NVDA": [{"t": "2026-07-06T13:30:00Z", "c": 100}, {"t": "2026-07-06T14:30:00Z", "c": 101}],
            "MLM": RuntimeError("alpaca unavailable"),
        })

        payload = service.snapshot(["NVDA", "MLM"], "1D")

        self.assertEqual(payload["items"][0]["symbol"], "NVDA")
        failed = next(item for item in payload["items"] if item["symbol"] == "MLM")
        self.assertEqual(failed["error"], "provider_error")
        self.assertTrue(payload["warnings"])

    def test_compare_cache_hit_skips_alpaca_fetcher(self):
        with mock.patch.dict(os.environ, {"CHART_COMPARE_CACHE_ENABLED": "true", "CHART_COMPARE_CACHE_TTL_1D_SECONDS": "60"}, clear=False):
            service = self.make_service({
                "NVDA": [{"t": "2026-07-06T13:30:00Z", "c": 100}, {"t": "2026-07-06T14:30:00Z", "c": 102}],
            })

            first = service.snapshot(["NVDA"], "1D")
            second = service.snapshot(["NVDA"], "1D")

        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
