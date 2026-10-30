import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.market_data.query import routes as query_routes  # noqa: E402
from app.market_data.query.service import MarketDataQueryService  # noqa: E402
from app.news.content import body_paragraphs  # noqa: E402
from app.news.hotness import NewsHotness  # noqa: E402
from app.services.simulation_guard import requires_point_in_time_data  # noqa: E402


class FakeNewsRepository:
    def __init__(self, localized=None, original=None):
        self.localized = localized or {}
        self.original = original or {}
        self.calls = []

    def localized_article(self, article_id, locale):
        self.calls.append(("localized", article_id, locale))
        return self.localized.get(article_id)

    def original_article(self, article_id):
        self.calls.append(("original", article_id))
        return self.original.get(article_id)


class LiveGateway:
    def status(self):
        return {"mode": "live"}


class SimulationGateway:
    def status(self):
        return {"mode": "simulation", "virtualTime": "2026-07-14T15:00:00Z"}


class NewsArticleRouteTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.repository = FakeNewsRepository(
            localized={
                "a1": {
                    "articleId": "a1",
                    "symbols": ["NVDA", "AMD"],
                    "targetSymbol": "NVDA",
                    "headline": "Nvidia earnings beat estimates",
                    "localizedHeadline": "엔비디아 실적, 예상 상회",
                    "localizedSummary": "데이터센터 매출이 크게 늘었습니다.",
                    "keyPoints": ["데이터센터 매출이 크게 늘었습니다.", "다음 분기 가이던스도 올렸습니다."],
                    "url": "https://example.com/a1",
                    "source": "Reuters",
                    "publishedAt": "2026-10-30T12:00:00.000Z",
                }
            },
            original={
                "b2": {
                    "articleId": "b2",
                    "symbol": "TSLA",
                    "headline": "Tesla faces lawsuit over Autopilot",
                    "summary": "<p>A new lawsuit was filed.</p>",
                    "url": "https://example.com/b2",
                    "source": "benzinga",
                    "publishedAt": "2026-10-29T09:00:00.000Z",
                    "raw": '{"symbols": ["TSLA"]}',
                    "category": "",
                }
            },
        )
        self.app = create_app()
        self.app.state.news_repository = self.repository
        self.app.state.simulator_gateway = LiveGateway()
        self.client = TestClient(self.app)

    def test_returns_localized_article_with_body_paragraphs(self):
        response = self.client.get("/api/news/a1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "id": "a1",
            "titleKo": "엔비디아 실적, 예상 상회",
            "source": "Reuters",
            "publishedAt": "2026-10-30T12:00:00.000Z",
            "url": "https://example.com/a1",
            "category": "earn",
            "relatedSymbols": [
                {"symbol": "NVDA", "nameKo": "엔비디아"},
                {"symbol": "AMD", "nameKo": "AMD"},
            ],
            "bodyKo": ["데이터센터 매출이 크게 늘었습니다.", "다음 분기 가이던스도 올렸습니다."],
        })
        self.assertEqual(self.repository.calls, [("localized", "a1", "ko-KR")])

    def test_falls_back_to_original_article_and_computes_category(self):
        response = self.client.get("/api/news/b2")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["titleKo"], "Tesla faces lawsuit over Autopilot")
        self.assertEqual(body["category"], "corp")
        self.assertEqual(body["relatedSymbols"][0]["symbol"], "TSLA")
        self.assertEqual(body["bodyKo"], ["A new lawsuit was filed."])

    def test_unknown_article_returns_404_code(self):
        response = self.client.get("/api/news/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": {"code": "news_not_found"}})

    def test_news_route_is_not_under_market_prefix(self):
        paths = {route.path for route in self.app.routes}
        self.assertIn("/api/news/{article_id}", paths)
        self.assertNotIn("/api/market/news/{article_id}", paths)

    def test_simulation_blocks_article_body(self):
        self.assertTrue(requires_point_in_time_data("/api/news/a1"))
        self.app.state.simulator_gateway = SimulationGateway()

        response = self.client.get("/api/news/a1")

        self.assertEqual(response.status_code, 409)

    def test_body_paragraphs_skip_duplicate_key_points(self):
        self.assertEqual(
            body_paragraphs({"localizedSummary": "요약 문장.", "keyPoints": ["요약 문장.", "둘째 문단"]}),
            ["요약 문장.", "둘째 문단"],
        )


class FakeLocalizedProvider:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def localized_news_articles_for_symbols(self, symbols, limit=10, days=None, locale="ko-KR"):
        self.calls.append({"symbols": list(symbols), "limit": limit})
        wanted = set(symbols)
        return [row for row in self.rows if set(row.get("symbols") or []) & wanted][:limit]


class FakeProvider:
    def __init__(self, rows):
        self.redis_provider = None
        self.clickhouse_provider = FakeLocalizedProvider(rows)


NEWS_ROWS = [
    {
        "articleId": "n1",
        "targetSymbol": "NVDA",
        "symbols": ["NVDA"],
        "headline": "Nvidia earnings beat estimates",
        "localizedHeadline": "엔비디아 실적 호조",
        "source": "Reuters",
        "publishedAt": "2026-10-30T12:00:00.000Z",
        "category": "earn",
    },
    {
        # 0002 마이그레이션 이전 행: category 가 비어 있어 읽을 때 분류한다.
        "articleId": "n2",
        "targetSymbol": "AMD",
        "symbols": ["AMD", "NVDA"],
        "headline": "AMD quarterly results top forecasts",
        "localizedHeadline": "AMD 분기 실적 예상 상회",
        "publishedAt": "2026-10-30T11:00:00.000Z",
    },
    {
        "articleId": "n3",
        "targetSymbol": "NVDA",
        "symbols": ["NVDA"],
        "headline": "Fed signals rate cut in December",
        "publishedAt": "2026-10-30T10:00:00.000Z",
    },
]


def make_service(rows=NEWS_ROWS, hot_today=None, averages=None):
    hotness = NewsHotness(
        today_volumes=lambda: hot_today or {},
        average_volumes=lambda symbols: {s: v for s, v in (averages or {}).items() if s in symbols},
    )
    return MarketDataQueryService(
        FakeProvider(rows),
        backfill_service=object(),
        fill_service=object(),
        canonical_query=object(),
        derived_service=object(),
        news_hotness=hotness,
    )


class LatestNewsFiltersTest(unittest.TestCase):
    def test_items_have_d13_fields(self):
        payload = make_service().latest_news("nvda", limit=5)

        item = payload["items"][0]
        self.assertEqual(item["category"], "earn")
        self.assertEqual(item["titleKo"], "엔비디아 실적 호조")
        self.assertEqual(item["relatedSymbols"], [{"symbol": "NVDA", "nameKo": "엔비디아"}])
        self.assertEqual(item["source"], "Reuters")
        self.assertEqual(item["publishedAt"], "2026-10-30T12:00:00.000Z")
        self.assertFalse(item["isHot"])
        # 기존 필드 유지
        self.assertEqual(item["title"], "엔비디아 실적 호조")
        self.assertEqual(item["articleId"], "n1")

    def test_category_filter_includes_rows_classified_at_read_time(self):
        payload = make_service().latest_news(symbols=["NVDA", "AMD"], category="earn", limit=10)

        self.assertEqual([item["articleId"] for item in payload["items"]], ["n1", "n2"])
        self.assertEqual(payload["category"], "earn")
        self.assertEqual(payload["symbols"], ["NVDA", "AMD"])

        rate = make_service().latest_news("NVDA", category="rate", limit=10)
        self.assertEqual([item["articleId"] for item in rate["items"]], ["n3"])

    def test_category_filter_overfetches_before_filtering(self):
        service = make_service()
        service.latest_news("NVDA", category="rate", limit=2)
        self.assertEqual(service.provider.clickhouse_provider.calls[0]["limit"], 6)

    def test_is_hot_when_any_related_symbol_volume_ratio_reaches_threshold(self):
        service = make_service(hot_today={"AMD": 30_000_000, "NVDA": 100}, averages={"AMD": 20_000_000, "NVDA": 1000})
        payload = service.latest_news(symbols=["NVDA", "AMD"], limit=10)

        hot = {item["articleId"]: item["isHot"] for item in payload["items"]}
        self.assertEqual(hot, {"n1": False, "n2": True, "n3": False})

    def test_route_parses_symbols_and_category(self):
        captured = {}

        class Service:
            def latest_news(self, symbol, **kwargs):
                captured.update(symbol=symbol, **kwargs)
                return {"items": []}

        app = create_app()
        app.state.simulator_gateway = LiveGateway()
        with mock.patch.object(query_routes, "get_query_service", return_value=Service()):
            client = TestClient(app)
            ok = client.get("/api/market/news/latest?category=earn&symbols=nvda, AMD,nvda&limit=20")
            bad = client.get("/api/market/news/latest?category=sports&symbols=NVDA")

        self.assertEqual(ok.status_code, 200)
        self.assertIsNone(captured["symbol"])
        self.assertEqual(captured["symbols"], ["NVDA", "AMD"])
        self.assertEqual(captured["category"], "earn")
        self.assertEqual(captured["limit"], 20)
        self.assertEqual(bad.status_code, 422)


class NewsHotnessTest(unittest.TestCase):
    def test_symbols_without_today_volume_skip_average_lookup(self):
        calls = []
        hotness = NewsHotness(today_volumes=lambda: {}, average_volumes=lambda s: calls.append(s) or {})
        self.assertEqual(hotness.hot_symbols(["NVDA"]), set())
        self.assertEqual(calls, [])

    def test_missing_average_or_failures_are_not_hot(self):
        hotness = NewsHotness(today_volumes=lambda: {"NVDA": 10.0}, average_volumes=lambda s: {"NVDA": None})
        self.assertEqual(hotness.hot_symbols(["NVDA"]), set())

        def boom(_symbols):
            raise RuntimeError("clickhouse down")

        self.assertEqual(NewsHotness(today_volumes=lambda: {"NVDA": 10.0}, average_volumes=boom).hot_symbols(["NVDA"]), set())

    def test_default_hotness_reuses_stocks_average_volume_cache(self):
        from app.stocks.service import AVG_VOLUME_KEY_PREFIX

        class Redis:
            def __init__(self):
                self.data = {AVG_VOLUME_KEY_PREFIX + "NVDA": '{"avg": 100.0}'}

            def get(self, key):
                return self.data.get(key)

            def set(self, key, value, ex=None):
                self.data[key] = value

        provider = types.SimpleNamespace(
            redis_provider=types.SimpleNamespace(redis=Redis()),
            clickhouse_provider=types.SimpleNamespace(recent_daily_volumes=lambda symbols, days=20: self.fail("cached")),
        )
        from app.news.hotness import news_hotness_for_provider

        hotness = news_hotness_for_provider(provider)
        hotness._today_volumes = lambda: {"NVDA": 160.0}
        self.assertEqual(hotness.hot_symbols(["NVDA"]), {"NVDA"})


if __name__ == "__main__":
    unittest.main()
