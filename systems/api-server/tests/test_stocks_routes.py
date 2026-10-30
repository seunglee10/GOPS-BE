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
        self.app = create_app()
        self.app.state.stocks_service = self.build_service()
        self.client = TestClient(self.app)

    def build_service(self):
        return stocks_service.StocksService(
            heatmap_provider=lambda: json.loads(json.dumps(HEATMAP)),
            daily_volumes_reader=self.read_volumes,
            redis_client=self.redis,
        )

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
