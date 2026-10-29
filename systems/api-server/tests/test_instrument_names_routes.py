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
    from app.market_data.query import routes as query_routes
    from app.market_data.query.service import MarketDataQueryService

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False

from app.market_data.heatmap.service import compact_heatmap_payload  # noqa: E402
from app.services.instrument_names import exchange_ko, korean_name_fields, name_ko  # noqa: E402
from market_data.reference import load_instrument_names_ko  # noqa: E402


class FakeProvider:
    def search_symbols(self, query, limit=20):
        records = [
            {"symbol": "NVDA", "name": "Nvidia", "exchange": "NASDAQ"},
            {"symbol": "ZZZZ", "name": "Unknown Co", "exchange": "NYSE"},
        ]
        normalized = query.upper()
        return [item for item in records if normalized in f"{item['symbol']} {item['name']}".upper()][:limit]

    def symbol_detail(self, symbol):
        if symbol == "QQQQ":
            raise LookupError(f"Unknown market symbol: {symbol}")
        return {"symbol": symbol, "name": symbol, "exchange": "NASDAQ", "tradable": True}


class FakeBackfillService:
    pass


class FakeFillService:
    pass


class InstrumentNamesSeedTest(unittest.TestCase):
    def test_seed_covers_sp500_universe_without_blank_names(self):
        import json

        names = load_instrument_names_ko()
        universe = json.loads((ROOT / "systems" / "market-data" / "config" / "sp500-universe.json").read_text(encoding="utf-8"))
        heatmap_seed = json.loads((ROOT / "systems" / "market-data" / "config" / "sp500-heatmap-seed.json").read_text(encoding="utf-8"))
        expected = set(universe["symbols"]) | {item["symbol"] for item in heatmap_seed["items"]}
        self.assertEqual(sorted(expected - set(names)), [])
        for symbol, entry in names.items():
            self.assertTrue(entry["nameKo"], symbol)
            self.assertIn(entry["exchangeKo"], {"나스닥", "NYSE", "CBOE"}, symbol)

    def test_lookup_is_case_insensitive_and_null_for_unknown(self):
        self.assertEqual(name_ko("nvda"), "엔비디아")
        self.assertEqual(exchange_ko("NVDA"), "나스닥")
        self.assertEqual(name_ko("BRK.B"), "버크셔 해서웨이")
        self.assertEqual(exchange_ko("JPM"), "NYSE")
        self.assertIsNone(name_ko("ZZZZ"))
        self.assertIsNone(exchange_ko(""))
        self.assertEqual(korean_name_fields(None), {"nameKo": None, "exchangeKo": None})

    def test_compact_heatmap_items_carry_korean_names(self):
        compact = compact_heatmap_payload({
            "items": [
                {"symbol": "MSFT", "companyName": "Microsoft", "marketCap": 1},
                {"symbol": "ZZZZ", "companyName": "Unknown", "marketCap": 1},
            ],
        })
        self.assertEqual(compact["items"][0]["nameKo"], "마이크로소프트")
        self.assertEqual(compact["items"][0]["exchangeKo"], "나스닥")
        self.assertIsNone(compact["items"][1]["nameKo"])
        self.assertIsNone(compact["items"][1]["exchangeKo"])


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class InstrumentNamesRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.previous_query_service = query_routes.get_query_service
        query_routes.get_query_service = lambda: MarketDataQueryService(
            FakeProvider(),
            backfill_service=FakeBackfillService(),
            fill_service=FakeFillService(),
        )
        self.client = TestClient(create_app())

    def tearDown(self):
        query_routes.get_query_service = self.previous_query_service

    def test_symbol_detail_returns_korean_name_for_nvda(self):
        response = self.client.get("/api/market/symbols/NVDA")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["nameKo"], "엔비디아")
        self.assertEqual(payload["exchangeKo"], "나스닥")
        self.assertEqual(payload["exchange"], "NASDAQ")

    def test_symbol_detail_returns_null_when_not_seeded(self):
        response = self.client.get("/api/market/symbols/ZZZZ")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("nameKo", payload)
        self.assertIsNone(payload["nameKo"])
        self.assertIsNone(payload["exchangeKo"])

    def test_symbol_detail_unknown_symbol_still_404(self):
        response = self.client.get("/api/market/symbols/QQQQ")
        self.assertEqual(response.status_code, 404)

    def test_symbol_search_items_carry_korean_names(self):
        response = self.client.get("/api/market/symbols/search?q=&limit=10")
        self.assertEqual(response.status_code, 200)
        by_symbol = {item["symbol"]: item for item in response.json()["symbols"]}
        self.assertEqual(by_symbol["NVDA"]["nameKo"], "엔비디아")
        self.assertEqual(by_symbol["NVDA"]["exchangeKo"], "나스닥")
        self.assertIsNone(by_symbol["ZZZZ"]["nameKo"])
        self.assertIsNone(by_symbol["ZZZZ"]["exchangeKo"])

    def test_symbol_page_items_carry_korean_names(self):
        from app.services import alfaka_market_data as market_data_service

        previous_universe = market_data_service.sp500_universe_symbols
        previous_summaries = market_data_service.symbol_summaries_for
        market_data_service.sp500_universe_symbols = lambda **_kwargs: ["NVDA", "ZZZZ"]
        market_data_service.symbol_summaries_for = lambda symbols, **_kwargs: [
            {"symbol": symbol, "name": symbol, "market": "US", "lastPrice": None, "changePercent": None}
            for symbol in symbols
        ]
        try:
            response = self.client.get("/api/market/symbols?page=1&pageSize=10")
        finally:
            market_data_service.sp500_universe_symbols = previous_universe
            market_data_service.symbol_summaries_for = previous_summaries

        self.assertEqual(response.status_code, 200)
        items = response.json()["symbols"]
        self.assertEqual([item["symbol"] for item in items], ["NVDA", "ZZZZ"])
        self.assertEqual(items[0]["nameKo"], "엔비디아")
        self.assertEqual(items[0]["exchangeKo"], "나스닥")
        self.assertIsNone(items[1]["nameKo"])
        self.assertEqual(items[0]["name"], "NVDA")


if __name__ == "__main__":
    unittest.main()
