from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from gops_agents.summaries import (
    MarketBriefGenerator,
    MarketBriefInput,
    StockSummaryGenerator,
    StockSummaryInput,
    SummaryModelError,
    build_summary_model_client_from_env,
    market_brief_key,
    stock_summary_key,
)
from gops_agents.summaries.llm import OpenAIResponsesJsonClient
from gops_agents.summaries.refresh import (
    MarketBriefArchive,
    RefreshConfig,
    brief_market_date,
    recently_generated,
    refresh_market_brief,
    refresh_stock_summaries,
    select_symbols,
)

NOW = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)  # 17:00 ET, 장 마감 뒤


class FakeModel:
    model = "fake"

    def __init__(self, output=None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = []

    def generate_json(self, *, name, instructions, payload, schema):
        self.calls.append({"name": name, "payload": payload, "schema": schema})
        if self.error:
            raise self.error
        return self.output


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttl[key] = ex
        return True


def candles(last_close=112.0, last_volume=2100.0):
    rows = [{"timestamp": f"2026-10-{day:02d}T00:00:00.000Z", "close": 100 + day * 0.5, "volume": 1000.0} for day in range(1, 20)]
    rows.append({"timestamp": "2026-10-30T00:00:00.000Z", "close": last_close, "volume": last_volume})
    return rows


def stock_input(**overrides):
    values = dict(
        symbol="nvda",
        news=[
            {"newsId": "n_1", "title": "차세대 GPU 출하 앞당겨", "summary": "출하 시점이 한 분기 앞당겨졌어요."},
            {"newsId": "n_2", "title": "수출 규제 세부안", "summary": "규제 대상 칩은 아직 공개되지 않았어요.", "impactDirection": "negative"},
        ],
        candles=candles(),
        events=[{"title": "실적 발표", "at": "2026-11-20", "note": "시장 예상 EPS 0.85달러"}],
    )
    values.update(overrides)
    return StockSummaryInput(**values)


VALID_STOCK_OUTPUT = {
    "head": "차세대 GPU 출하가 앞당겨진다는 보고서가 나온 날이에요. 거래가 평소의 두 배 몰렸어요.",
    "news": [{"text": "공급망 조사에서 출하 시점이 한 분기 이르게 잡혔어요.", "newsId": "n_1"}],
    "risks": [{"text": "수출 규제 세부안이 연말까지 나와요.", "newsId": "n_2"}],
    "events": [{"title": "실적 발표", "at": "2026-11-20", "note": "시장 예상 매출 335억 달러"}],
    "chartNote": "20일선 위에서 거래량 늘며 상승",
}


class StockSummaryGeneratorTest(unittest.TestCase):
    def test_valid_llm_json_is_used(self):
        model = FakeModel(VALID_STOCK_OUTPUT)
        summary, used = StockSummaryGenerator(model, now=lambda: NOW).generate_with_source(stock_input())
        self.assertEqual(used, "llm")
        self.assertEqual(summary["symbol"], "NVDA")
        self.assertEqual(summary["generatedAt"], "2026-10-30T21:00:00Z")
        self.assertEqual(summary["head"], VALID_STOCK_OUTPUT["head"])
        self.assertEqual(summary["news"], VALID_STOCK_OUTPUT["news"])
        self.assertEqual(summary["events"][0]["note"], "시장 예상 매출 335억 달러")
        self.assertEqual(set(summary), {"symbol", "generatedAt", "head", "news", "risks", "events", "chartNote"})
        payload = model.calls[0]["payload"]
        self.assertTrue(payload["chart"]["aboveSma20"])
        self.assertEqual(payload["chart"]["volumeRatio20d"], 2.1)
        self.assertTrue(model.calls[0]["schema"]["additionalProperties"] is False)

    def test_invalid_json_falls_back_to_template(self):
        cases = [
            {"head": "not an object"},  # 필수 키 없음
            {**VALID_STOCK_OUTPUT, "news": [{"text": "없는 기사예요.", "newsId": "n_999"}]},
            {**VALID_STOCK_OUTPUT, "head": "Shares rose."},  # 해요체 아님
            {**VALID_STOCK_OUTPUT, "head": "앞으로 오를 것으로 보여요."},  # 예측 문장
            {**VALID_STOCK_OUTPUT, "events": [{"title": "x", "at": "2027-01-01", "note": ""}]},
            "not json",
        ]
        for output in cases:
            with self.subTest(output=output):
                summary, used = StockSummaryGenerator(FakeModel(output), now=lambda: NOW).generate_with_source(stock_input())
                self.assertEqual(used, "template")
                self.assertTrue(summary["head"])

    def test_model_error_falls_back_to_template(self):
        model = FakeModel(error=SummaryModelError("boom", code="provider_timeout"))
        summary, used = StockSummaryGenerator(model, now=lambda: NOW).generate_with_source(stock_input())
        self.assertEqual(used, "template")
        self.assertIn("20일 평균의 2.1배", summary["head"])

    def test_template_uses_chart_facts_and_splits_risks(self):
        summary = StockSummaryGenerator(None, now=lambda: NOW).generate(stock_input())
        self.assertTrue(summary["head"].startswith("전 거래일보다 "))
        self.assertIn("올랐고", summary["head"])
        self.assertEqual(summary["chartNote"], "20일선 위에서 거래량 늘며 상승")
        self.assertEqual([item["newsId"] for item in summary["news"]], ["n_1"])
        self.assertEqual([item["newsId"] for item in summary["risks"]], ["n_2"])
        self.assertEqual(summary["events"], [{"title": "실적 발표", "at": "2026-11-20", "note": "시장 예상 EPS 0.85달러"}])
        for text in [summary["head"], *(item["text"] for item in summary["news"] + summary["risks"])]:
            self.assertTrue(text.rstrip(".").endswith("요"), text)

    def test_template_below_sma_with_low_volume(self):
        summary = StockSummaryGenerator(None, now=lambda: NOW).generate(stock_input(candles=candles(last_close=95.0, last_volume=500.0), news=[]))
        self.assertEqual(summary["chartNote"], "20일선 아래에서 거래량 줄며 하락")
        self.assertIn("내렸고", summary["head"])
        self.assertIn("이 종목만 다룬 새 기사는 없어요.", summary["head"])

    def test_no_candles_gives_null_chart_note(self):
        summary = StockSummaryGenerator(None, now=lambda: NOW).generate(stock_input(candles=[]))
        self.assertIsNone(summary["chartNote"])
        self.assertFalse(stock_input(candles=[], news=[]).has_data())


VALID_BRIEF_OUTPUT = {
    "paragraphs": [
        {"text": "반도체가 지수 상승을 이끌었어요.", "refs": [{"kind": "symbol", "value": "NVDA"}, {"kind": "macro", "value": "^GSPC"}]},
        {"text": "연준 의사록이 많이 다뤄졌어요.", "refs": [{"kind": "news", "value": "a_1"}]},
    ]
}


def brief_input():
    return MarketBriefInput(
        market_date="2026-10-30",
        indices=[
            {"symbol": "^GSPC", "nameKo": "S&P 500", "price": 5800.0, "changePercent": 0.8},
            {"symbol": "^IXIC", "nameKo": "나스닥 종합", "price": 18000.0, "changePercent": 1.2},
            {"symbol": "^VIX", "nameKo": "VIX", "price": 15.31, "changePercent": -3.0},
            {"symbol": "^KS11", "nameKo": "코스피", "price": 2600.0, "changePercent": 0.1},
        ],
        heatmap_items=[
            {"symbol": "NVDA", "sectorLabelKo": "정보기술", "changePercent": 4.2, "marketCap": 3e12, "sessionDollarVolume": 3e10},
            {"symbol": "AAPL", "sectorLabelKo": "정보기술", "changePercent": 0.3, "marketCap": 3e12, "sessionDollarVolume": 1e10},
            {"symbol": "XOM", "sectorLabelKo": "에너지", "changePercent": -1.1, "marketCap": 5e11, "sessionDollarVolume": 2e9},
        ],
        news=[{"newsId": "a_1", "title": "연준 의사록 공개", "summary": "", "symbols": ["NVDA"]}],
    )


class MarketBriefGeneratorTest(unittest.TestCase):
    def test_valid_llm_json_is_used(self):
        brief, used = MarketBriefGenerator(FakeModel(VALID_BRIEF_OUTPUT), now=lambda: NOW).generate_with_source(brief_input())
        self.assertEqual(used, "llm")
        self.assertEqual(brief["marketDate"], "2026-10-30")
        self.assertEqual(brief["paragraphs"], VALID_BRIEF_OUTPUT["paragraphs"])

    def test_unknown_ref_or_bad_text_falls_back(self):
        cases = [
            {"paragraphs": [{"text": "올랐어요.", "refs": [{"kind": "symbol", "value": "ZZZZ"}]}]},
            {"paragraphs": [{"text": "Stocks rose.", "refs": []}]},
            {"paragraphs": []},
            {"wrong": True},
        ]
        for output in cases:
            with self.subTest(output=output):
                brief, used = MarketBriefGenerator(FakeModel(output), now=lambda: NOW).generate_with_source(brief_input())
                self.assertEqual(used, "template")
                self.assertTrue(brief["paragraphs"])

    def test_template_paragraphs_have_refs(self):
        brief = MarketBriefGenerator(None, now=lambda: NOW).generate(brief_input())
        texts = [p["text"] for p in brief["paragraphs"]]
        self.assertIn("S&P 500 지수는 +0.8%", texts[0])
        self.assertIn("VIX는 15.3 수준", texts[0])
        self.assertNotIn("코스피", texts[0])
        self.assertIn("가장 강한 곳은 정보기술", texts[1])
        self.assertIn("엔비디아 +4.2%", texts[1])
        self.assertIn("엑슨모빌 -1.1%예요.", texts[1])
        refs = {(r["kind"], r["value"]) for p in brief["paragraphs"] for r in p["refs"]}
        self.assertTrue({("macro", "^GSPC"), ("symbol", "NVDA"), ("news", "a_1")} <= refs)
        for text in texts:
            self.assertTrue(text.rstrip(".").endswith("요"), text)


class FakeSource:
    def __init__(self):
        self.news_by_symbol = {"NVDA": [{"newsId": "n_1", "title": "제목", "summary": "요약이에요."}]}
        self.calls: list[str] = []

    def news_symbols_since(self, hours):
        return ["nvda", "TSLA"]

    def latest_news(self, symbol, limit):
        self.calls.append(symbol)
        return self.news_by_symbol.get(symbol, [])

    def daily_candles(self, symbol, limit):
        return [] if symbol == "EMPTY" else candles()

    def upcoming_events(self, symbol):
        return []

    def heatmap_items(self):
        return [
            {"symbol": "AAPL", "marketCap": 3.5e12, "sectorLabelKo": "정보기술", "changePercent": 1.0},
            {"symbol": "NVDA", "marketCap": 3.4e12, "sectorLabelKo": "정보기술", "changePercent": 3.0},
            {"symbol": "MSFT", "marketCap": 3.0e12, "sectorLabelKo": "정보기술", "changePercent": 0.5},
            {"symbol": "XOM", "marketCap": 5e11, "sectorLabelKo": "에너지", "changePercent": -1.0},
        ]

    def index_items(self):
        return brief_input().indices

    def top_news(self, hours, limit):
        return brief_input().news


class RefreshJobTest(unittest.TestCase):
    def test_selection_puts_news_symbols_first_then_market_cap(self):
        config = RefreshConfig(top_market_cap=2)
        self.assertEqual(select_symbols(FakeSource(), config), ["NVDA", "TSLA", "AAPL"])

    def test_refresh_writes_cache_with_ttl_and_throttles_per_hour(self):
        redis_client = FakeRedis()
        source = FakeSource()
        generator = StockSummaryGenerator(None, now=lambda: NOW)
        config = RefreshConfig(top_market_cap=2)
        stats = refresh_stock_summaries(source, redis_client, generator, config=config, now=NOW)
        self.assertEqual(stats["written"], 3)
        self.assertEqual(stats["template"], 3)
        cached = json.loads(redis_client.values[stock_summary_key("NVDA")])
        self.assertEqual(cached["symbol"], "NVDA")
        self.assertIn("정보기술 업종", cached["head"])
        self.assertEqual(redis_client.ttl[stock_summary_key("NVDA")], 24 * 3600)

        later = NOW.replace(minute=30)
        again = refresh_stock_summaries(source, redis_client, generator, config=config, now=later)
        self.assertEqual(again["throttled"], 3)
        self.assertEqual(again["written"], 0)
        self.assertTrue(recently_generated(redis_client, "NVDA", now=later, min_interval_seconds=3600))
        self.assertFalse(recently_generated(redis_client, "NVDA", now=NOW.replace(hour=22, minute=1), min_interval_seconds=3600))

        forced = refresh_stock_summaries(source, redis_client, generator, config=config, now=later, symbols=["NVDA"], force=True)
        self.assertEqual(forced["written"], 1)

    def test_symbols_without_data_are_not_cached(self):
        redis_client = FakeRedis()
        stats = refresh_stock_summaries(
            FakeSource(), redis_client, StockSummaryGenerator(None), config=RefreshConfig(), now=NOW, symbols=["EMPTY"]
        )
        self.assertEqual(stats["noData"], 1)
        self.assertNotIn(stock_summary_key("EMPTY"), redis_client.values)

    def test_brief_runs_once_after_close(self):
        redis_client = FakeRedis()
        archive = FakeArchive()
        generator = MarketBriefGenerator(None, now=lambda: NOW)
        before_close = datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc)  # 15:00 ET
        skipped = refresh_market_brief(FakeSource(), redis_client, generator, now=before_close, archive=archive)
        self.assertEqual(skipped["reason"], "market_not_closed_today")

        written = refresh_market_brief(FakeSource(), redis_client, generator, now=NOW, archive=archive)
        self.assertEqual(written["status"], "written")
        self.assertEqual(written["marketDate"], "2026-10-30")
        self.assertEqual(redis_client.ttl[market_brief_key("2026-10-30")], 3 * 24 * 3600)
        self.assertEqual(archive.puts[0]["marketDate"], "2026-10-30")

        again = refresh_market_brief(FakeSource(), redis_client, generator, now=NOW, archive=archive)
        self.assertEqual(again["reason"], "already_generated")

    def test_brief_flag_uses_latest_closed_session(self):
        saturday = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
        self.assertIsNone(brief_market_date(saturday, force=False))
        self.assertEqual(brief_market_date(saturday, force=True), "2026-10-30")
        friday_morning = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(brief_market_date(friday_morning, force=True), "2026-10-29")

    def test_archive_key_and_disabled_without_bucket(self):
        archive = MarketBriefArchive(bucket="", prefix="market-brief/v1")
        self.assertFalse(archive.enabled())
        self.assertIsNone(archive.put({"marketDate": "2026-10-30"}))
        client = FakeS3()
        enabled = MarketBriefArchive(client=client, bucket="b", prefix="market-brief/v1")
        self.assertEqual(enabled.put({"marketDate": "2026-10-30", "paragraphs": []}), "s3://b/market-brief/v1/date=2026-10-30/brief.json")
        self.assertEqual(client.calls[0]["Key"], "market-brief/v1/date=2026-10-30/brief.json")


class FakeArchive:
    def __init__(self):
        self.puts = []

    def put(self, brief):
        self.puts.append(brief)
        return None


class FakeS3:
    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)


class ModelClientConfigTest(unittest.TestCase):
    def test_llm_disabled_by_default(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        self.assertIsNone(build_summary_model_client_from_env(env.get))
        env["STOCK_SUMMARY_LLM_ENABLED"] = "true"
        self.assertIsInstance(build_summary_model_client_from_env(env.get), OpenAIResponsesJsonClient)
        self.assertIsNone(build_summary_model_client_from_env({"STOCK_SUMMARY_LLM_ENABLED": "true"}.get))

    def test_openai_client_parses_structured_output_without_network(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"status": "completed", "output_text": json.dumps({"head": "올랐어요."})}).encode()

        def urlopen(request, timeout):
            captured["body"] = json.loads(request.data)
            return Response()

        client = OpenAIResponsesJsonClient(read_config={"OPENAI_API_KEY": "k", "STOCK_SUMMARY_MODEL": "m"}.get, urlopen=urlopen)
        self.assertEqual(client.generate_json(name="x", instructions="i", payload={"a": 1}, schema={"type": "object"}), {"head": "올랐어요."})
        self.assertEqual(captured["body"]["model"], "m")
        self.assertEqual(captured["body"]["text"]["format"]["type"], "json_schema")
        self.assertTrue(captured["body"]["text"]["format"]["strict"])


class FakeClickHouse:
    def __init__(self):
        self.queries = []

    def table(self, name):
        return f"market_data.{name}"

    def query_json_each_row(self, query, params):
        self.queries.append((query, params))
        if "yahoo_earnings_estimates" in query:
            return [{"at": "2026-11-20", "epsEstimate": 0.851}]
        if "GROUP BY target_symbol" in query:
            return [{"symbol": "nvda", "articles": 3}]
        return []

    def localized_news_articles(self, symbol, limit, days):
        return [{
            "articleId": "a1", "localizedHeadline": "한글 제목", "localizedSummary": "한글 요약이에요.",
            "publishedAt": "2026-10-30T12:00:00.000Z", "impactDirection": "negative", "concerns": ["규제 우려가 있어요."],
            "symbols": ["NVDA"],
        }]

    def daily_candles(self, symbol, interval, limit):
        return [*candles(), {"timestamp": "2026-10-31T00:00:00.000Z", "close": 1, "isClosed": False}]


class StoreSourceTest(unittest.TestCase):
    def test_reads_clickhouse_and_redis_snapshots(self):
        from gops_agents.summaries.sources import StoreSummarySource

        redis_client = FakeRedis()
        redis_client.values["gops:market:on-demand:v1:heatmap:sp500:last"] = json.dumps({"items": [{"symbol": "AAPL"}]})
        redis_client.values["gops:market:on-demand:v1:indices:snapshot"] = json.dumps({"items": [{"symbol": "^GSPC"}]})
        source = StoreSummarySource(clickhouse=FakeClickHouse(), redis_client=redis_client)
        self.assertEqual(source.news_symbols_since(24), ["NVDA"])
        news = source.latest_news("NVDA", 8)
        self.assertEqual(news[0]["newsId"], "a1")
        self.assertEqual(news[0]["title"], "한글 제목")
        self.assertEqual(news[0]["concerns"], ["규제 우려가 있어요."])
        self.assertEqual(len(source.daily_candles("NVDA", 20)), 20)
        self.assertEqual(source.upcoming_events("NVDA"), [{"title": "실적 발표", "at": "2026-11-20", "note": "시장 예상 EPS 0.85달러"}])
        self.assertEqual(source.heatmap_items(), [{"symbol": "AAPL"}])
        self.assertEqual(source.index_items(), [{"symbol": "^GSPC"}])


ROOT = Path(__file__).resolve().parents[3]
JOB_PATH = ROOT / "systems" / "agent-orchestration" / "jobs" / "stock-summary-refresh" / "main.py"


class JobEntrypointTest(unittest.TestCase):
    def test_args(self):
        spec = importlib.util.spec_from_file_location("stock_summary_refresh_main", JOB_PATH)
        assert spec and spec.loader
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
        args = job.parse_args(["--brief", "--symbols", "NVDA,amd", "--force"])
        self.assertTrue(args.brief)
        self.assertTrue(args.force)
        self.assertEqual(args.symbols, "NVDA,amd")


if __name__ == "__main__":
    unittest.main()
