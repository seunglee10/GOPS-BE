import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.instrument_names import name_ko
    from app.services.simulation_guard import requires_point_in_time_data
    from app.stocks import service as stocks_service

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True


def heatmap_item(symbol, *, price, change, volume, dollar, cap, sector="Information Technology", previous=None):
    return {
        "symbol": symbol,
        "sector": sector,
        "sectorLabelKo": "정보기술" if sector == "Information Technology" else "기타",
        "lastPrice": price,
        "previousClose": previous,
        "changePercent": change,
        "volume": volume,
        "sessionDollarVolume": dollar,
        "marketCap": cap,
    }


HEATMAP = {
    "quoteAsOf": "2026-10-30T20:00:00Z",
    "items": [
        heatmap_item("NVDA", price=142.3, change=2.12, volume=210_000_000, dollar=31_200_000_000, cap=3_480_000_000_000, previous=139.35),
        heatmap_item("AAPL", price=230.0, change=-1.5, volume=50_000_000, dollar=11_500_000_000, cap=3_500_000_000_000, previous=233.5),
        heatmap_item("AMD", price=150.0, change=4.8, volume=80_000_000, dollar=12_000_000_000, cap=240_000_000_000, previous=143.13),
        heatmap_item("AVGO", price=170.0, change=0.3, volume=30_000_000, dollar=5_100_000_000, cap=800_000_000_000, previous=169.49),
        heatmap_item("MSFT", price=410.0, change=-3.2, volume=25_000_000, dollar=10_250_000_000, cap=3_050_000_000_000, previous=423.55),
        heatmap_item("XOM", price=110.0, change=0.9, volume=15_000_000, dollar=1_650_000_000, cap=470_000_000_000, sector="Energy", previous=109.0),
        {"symbol": "NOPRICE", "sector": "Energy", "lastPrice": None, "volume": 999_999_999, "marketCap": 1_000_000},
    ],
}


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class StocksRoutesTestBase(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.redis = FakeRedis()
        self.volume_calls = []
        self.volumes = {
            "NVDA": [100_000_000.0] * 20,
            "AMD": [40_000_000.0] * 20,
            "AAPL": [50_000_000.0] * 20,
            "AVGO": [20_000_000.0] * 3,  # 5일 미만이면 평균을 내지 않는다
        }
        self.candle_calls = []
        self.eps_calls = []
        self.eps = {"NVDA": 2.6255, "AMD": 3.0, "AVGO": -1.0, "MSFT": 12.0}
        self.live_prices = {}
        self.app = create_app()
        self.app.state.stocks_service = self.build_service()
        self.client = TestClient(self.app)

    def build_service(self):
        return stocks_service.StocksService(
            heatmap_provider=lambda: json.loads(json.dumps(HEATMAP)),
            daily_volumes_reader=self.read_volumes,
            daily_candles_reader=self.read_candles,
            eps_reader=self.read_eps,
            live_price_reader=self.live_prices.get,
            redis_client=self.redis,
        )

    def read_candles(self, symbol, limit):
        self.candle_calls.append((symbol, limit))
        if symbol not in {"NVDA", "ZZZZ"}:
            return []
        rows = [
            {"timestamp": f"2026-10-{day:02d}T00:00:00.000Z", "open": 100, "high": 100 + day, "low": 75.61 + day, "close": 120, "volume": 1}
            for day in range(1, 29)
        ]
        rows[5]["high"] = 152.89
        rows[0]["low"] = 75.61
        rows.append({"timestamp": "2026-10-29T00:00:00.000Z", "open": 140, "high": 143.1, "low": 139.2, "close": 141.0, "volume": 200_000_000})
        return rows

    def read_eps(self, symbols):
        self.eps_calls.append(list(symbols))
        return {symbol: self.eps.get(symbol) for symbol in symbols}

    def read_volumes(self, symbols, days):
        self.volume_calls.append((list(symbols), days))
        return {symbol: self.volumes[symbol] for symbol in symbols if symbol in self.volumes}


class StockMoversRoutesTest(StocksRoutesTestBase):
    def symbols(self, sort, limit=20):
        response = self.client.get("/api/stocks/movers", params={"sort": sort, "limit": limit})
        self.assertEqual(response.status_code, 200)
        return [item["symbol"] for item in response.json()["items"]]

    def test_sort_orders(self):
        self.assertEqual(self.symbols("trade"), ["NVDA", "AMD", "AAPL", "MSFT", "AVGO", "XOM"])
        self.assertEqual(self.symbols("volume"), ["NVDA", "AMD", "AAPL", "AVGO", "MSFT", "XOM"])
        self.assertEqual(self.symbols("up"), ["AMD", "NVDA", "XOM", "AVGO", "AAPL", "MSFT"])
        self.assertEqual(self.symbols("down"), ["MSFT", "AAPL", "AVGO", "XOM", "NVDA", "AMD"])

    def test_limit_and_bad_sort(self):
        self.assertEqual(self.symbols("trade", limit=2), ["NVDA", "AMD"])
        self.assertEqual(self.client.get("/api/stocks/movers", params={"sort": "hot"}).status_code, 422)
        self.assertEqual(self.client.get("/api/stocks/movers", params={"limit": 0}).status_code, 422)

    def test_item_shape_ratio_and_null_issue(self):
        response = self.client.get("/api/stocks/movers", params={"sort": "trade", "limit": 5})
        body = response.json()
        self.assertEqual(body["asOf"], "2026-10-30T20:00:00Z")
        nvda = body["items"][0]
        self.assertEqual(set(nvda), {
            "symbol", "nameKo", "price", "changePercent", "dollarVolume", "volume",
            "volumeRatio20d", "issue", "issueNewsId", "chartNote",
        })
        self.assertEqual(nvda["nameKo"], "엔비디아")
        self.assertEqual(nvda["price"], 142.3)
        self.assertEqual(nvda["dollarVolume"], 31_200_000_000)
        self.assertEqual(nvda["volumeRatio20d"], 2.1)
        self.assertIsNone(nvda["issue"])
        self.assertIsNone(nvda["issueNewsId"])
        self.assertIsNone(nvda["chartNote"])
        by_symbol = {item["symbol"]: item for item in body["items"]}
        self.assertEqual(by_symbol["AMD"]["volumeRatio20d"], 2.0)
        self.assertEqual(by_symbol["AAPL"]["volumeRatio20d"], 1.0)
        self.assertIsNone(by_symbol["AVGO"]["volumeRatio20d"])
        self.assertIsNone(by_symbol["MSFT"]["volumeRatio20d"])

    def test_issue_fields_from_ai_summary_cache(self):
        self.redis.set("gops:stocks:ai-summary:NVDA", json.dumps({
            "head": "데이터센터 수요 전망 상향",
            "news": [{"text": "...", "newsId": "n_123"}],
            "chartNote": "20일선 위에서 거래량 늘며 상승",
        }, ensure_ascii=False))
        self.redis.set("gops:stocks:ai-summary:AMD", "not-json")
        items = self.client.get("/api/stocks/movers", params={"limit": 2}).json()["items"]
        self.assertEqual(items[0]["issue"], "데이터센터 수요 전망 상향")
        self.assertEqual(items[0]["issueNewsId"], "n_123")
        self.assertEqual(items[0]["chartNote"], "20일선 위에서 거래량 늘며 상승")
        self.assertIsNone(items[1]["issue"])

    def test_average_volume_is_batched_and_cached(self):
        self.client.get("/api/stocks/movers", params={"limit": 3})
        self.assertEqual(len(self.volume_calls), 1)
        self.assertEqual(sorted(self.volume_calls[0][0]), ["AAPL", "AMD", "NVDA"])
        self.assertEqual(self.volume_calls[0][1], 20)
        self.assertEqual(self.redis.expirations["gops:stocks:avg-volume-20d:NVDA"], 3600)
        self.client.get("/api/stocks/movers", params={"limit": 3})
        self.assertEqual(len(self.volume_calls), 1)

    def test_simulation_guard_blocks_stocks(self):
        self.assertTrue(requires_point_in_time_data("/api/stocks/movers"))
        self.assertTrue(requires_point_in_time_data("/api/stocks/NVDA/summary"))


class StockSummaryRoutesTest(StocksRoutesTestBase):
    def test_summary_shape_matches_contract(self):
        response = self.client.get("/api/stocks/nvda/summary")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {
            "symbol", "nameKo", "exchangeKo", "price", "change", "changePercent", "marketCap", "marketCapRank",
            "per", "sectorPer", "sectorLabelKo", "low52", "high52", "dayLow", "dayHigh", "volume",
            "avgVolume20d", "volumeRatio20d", "dollarVolume", "peers", "asOf",
        })
        self.assertEqual(body["symbol"], "NVDA")
        self.assertEqual(body["nameKo"], "엔비디아")
        self.assertEqual(body["exchangeKo"], "나스닥")
        self.assertEqual(body["price"], 142.3)
        self.assertAlmostEqual(body["change"], 2.95)
        self.assertEqual(body["changePercent"], 2.12)
        self.assertEqual(body["marketCap"], 3_480_000_000_000)
        self.assertEqual(body["marketCapRank"], 2)  # AAPL 3.5T 다음
        self.assertEqual(body["per"], 54.2)
        self.assertEqual(body["sectorLabelKo"], "정보기술")
        self.assertEqual(body["low52"], 75.61)
        self.assertEqual(body["high52"], 152.89)
        self.assertEqual(body["dayLow"], 139.2)
        self.assertEqual(body["dayHigh"], 143.1)
        self.assertEqual(body["volume"], 210_000_000)
        self.assertEqual(body["avgVolume20d"], 100_000_000)
        self.assertEqual(body["volumeRatio20d"], 2.1)
        self.assertEqual(body["dollarVolume"], 31_200_000_000)
        self.assertEqual(body["asOf"], "2026-10-30T20:00:00Z")
        self.assertEqual(self.candle_calls, [("NVDA", 252)])

    def test_peers_exclude_self_and_sector_per_is_median(self):
        body = self.client.get("/api/stocks/NVDA/summary").json()
        # 정보기술 시총 순: AAPL, NVDA, MSFT, AVGO, AMD → 자기 자신 제외 상위 3개
        self.assertEqual([peer["symbol"] for peer in body["peers"]], ["AAPL", "MSFT", "AVGO"])
        self.assertEqual(body["peers"][1], {"symbol": "MSFT", "nameKo": name_ko("MSFT"), "per": 34.17})
        self.assertIsNone(body["peers"][0]["per"])  # EPS 없음
        self.assertIsNone(body["peers"][2]["per"])  # 적자 EPS
        # PER 있는 종목: NVDA 54.2, MSFT 34.17, AMD 50.0 → 중앙값 50.0
        self.assertEqual(body["sectorPer"], 50.0)
        self.assertEqual(len(self.eps_calls), 1)
        self.assertEqual(sorted(self.eps_calls[0]), ["AAPL", "AMD", "AVGO", "MSFT", "NVDA"])

    def test_per_null_when_eps_missing(self):
        self.eps = {}
        response = self.client.get("/api/stocks/NVDA/summary")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["per"])
        self.assertIsNone(response.json()["sectorPer"])

    def test_eps_reader_failure_does_not_500(self):
        def broken(_symbols):
            raise RuntimeError("clickhouse down")

        self.app.state.stocks_service.eps_reader = broken
        response = self.client.get("/api/stocks/NVDA/summary")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["per"])

    def test_unknown_symbol_is_404(self):
        response = self.client.get("/api/stocks/QQQQ/summary")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "symbol_not_found")
        response = self.client.get("/api/stocks/bad$sym/summary")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "symbol_not_found")

    def test_live_price_overrides_projection(self):
        self.live_prices["NVDA"] = 150.0
        body = self.client.get("/api/stocks/NVDA/summary").json()
        self.assertEqual(body["price"], 150.0)
        self.assertEqual(body["changePercent"], 7.64)
        self.assertEqual(body["dayHigh"], 150.0)

    def test_symbol_only_in_candles_still_returns(self):
        body = self.client.get("/api/stocks/ZZZZ/summary").json()
        self.assertEqual(body["price"], 141.0)
        self.assertEqual(body["change"], 21.0)
        self.assertIsNone(body["marketCap"])
        self.assertIsNone(body["marketCapRank"])
        self.assertEqual(body["peers"], [])

    def test_summary_cache_hit(self):
        first = self.client.get("/api/stocks/NVDA/summary").json()
        self.assertEqual(self.redis.expirations["gops:stocks:summary:NVDA"], 60)
        self.live_prices["NVDA"] = 999.0
        second = self.client.get("/api/stocks/NVDA/summary").json()
        self.assertEqual(first, second)
        self.assertEqual(len(self.candle_calls), 1)


class TrailingEpsTest(unittest.TestCase):
    TODAY = __import__("datetime").date(2026, 10, 30)

    def test_sums_latest_four_quarters(self):
        rows = [
            {"fiscalPeriod": "Q3", "periodEndDate": "2026-09-30", "value": 1.0},
            {"fiscalPeriod": "Q2", "periodEndDate": "2026-06-30", "value": 0.9},
            {"fiscalPeriod": "Q1", "periodEndDate": "2026-03-31", "value": 0.8},
            {"fiscalPeriod": "Q4", "periodEndDate": "2025-12-31", "value": 0.7},
            {"fiscalPeriod": "Q3", "periodEndDate": "2025-09-30", "value": 0.6},
            {"fiscalPeriod": "FY", "periodEndDate": "2025-12-31", "value": 2.5},
        ]
        self.assertEqual(stocks_service.trailing_eps(rows, today=self.TODAY), 3.4)

    def test_prefers_annual_when_it_is_latest(self):
        rows = [
            {"fiscalPeriod": "Q4", "periodEndDate": "2026-06-30", "value": 0.7},
            {"fiscalPeriod": "Q3", "periodEndDate": "2026-03-31", "value": 2.1},
            {"fiscalPeriod": "FY", "periodEndDate": "2026-06-30", "value": 3.0},
        ]
        self.assertEqual(stocks_service.trailing_eps(rows, today=self.TODAY), 3.0)

    def test_none_when_stale_or_missing(self):
        self.assertIsNone(stocks_service.trailing_eps([], today=self.TODAY))
        rows = [{"fiscalPeriod": "FY", "periodEndDate": "2024-12-31", "value": 3.0}]
        self.assertIsNone(stocks_service.trailing_eps(rows, today=self.TODAY))

    def test_clickhouse_reader_queries_facts_once(self):
        calls = []

        class FakeClickHouse:
            def table(self, name):
                return f"market_data.{name}"

            def query_json_each_row(self, sql, params):
                calls.append((sql, params))
                return [
                    {"symbol": "NVDA", "fiscalPeriod": "FY", "periodEndDate": "2026-01-25", "value": 2.94},
                    {"symbol": "nvda", "fiscalPeriod": "Q1", "periodEndDate": "2025-04-27", "value": 0.76},
                ]

        result = stocks_service.clickhouse_trailing_eps(FakeClickHouse(), ["NVDA", "AMD"])
        self.assertEqual(result, {"NVDA": 2.94, "AMD": None})
        self.assertEqual(len(calls), 1)
        self.assertIn("market_data.sec_financial_facts", calls[0][0])
        self.assertIn("metric = 'eps'", calls[0][0])


class RecentDailyVolumesQueryTest(unittest.TestCase):
    def test_batches_symbols_and_groups_rows(self):
        from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider

        provider = ClickHouseMarketDataProvider.__new__(ClickHouseMarketDataProvider)
        provider.database = "market_data"
        captured = {}

        def query(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return [
                {"symbol": "NVDA", "volume": "100"},
                {"symbol": "NVDA", "volume": 200},
                {"symbol": "AMD", "volume": None},
            ]

        provider.query_json_each_row = query
        result = provider.recent_daily_volumes(["NVDA", "AMD", "NVDA"], days=20)
        self.assertEqual(result, {"NVDA": [100.0, 200.0]})
        self.assertEqual(captured["params"], {"symbols": ["NVDA", "AMD"]})
        self.assertIn("LIMIT 20 BY symbol", captured["sql"])
        self.assertIn("is_closed = true", captured["sql"])


if __name__ == "__main__":
    unittest.main()
