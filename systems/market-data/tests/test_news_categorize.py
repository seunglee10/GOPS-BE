from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from market_data.alpaca.news import build_news_event
from market_data.news.categorize import NEWS_CATEGORIES, categorize_news, normalize_news_category
from market_data.storage.clickhouse_loader import news_to_clickhouse_row
from market_data.storage.news_intelligence import build_news_intelligence_record, news_intelligence_to_clickhouse_row

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Nvidia Q3 earnings beat estimates as data center revenue soars", "earn"),
        ("Apple raises its outlook after record iPhone sales", "earn"),
        ("엔비디아 3분기 실적, 시장 예상 웃돌아", "earn"),
        ("Fed holds interest rates steady, Powell signals patience", "rate"),
        ("CPI report shows inflation cooling in September", "rate"),
        ("미 국채 10년물 금리 4.5% 돌파", "rate"),
        ("Semiconductor ETFs see record inflows this week", "etf"),
        ("반도체 ETF로 자금 유입 이어져", "etf"),
        ("China retaliates with new tariffs on U.S. chips", "global"),
        ("유럽 증시, 일본 엔화 약세에 혼조", "global"),
        ("Microsoft to acquire gaming studio in $2 billion deal", "corp"),
        ("Tesla faces lawsuit over Autopilot crash", "corp"),
        ("애플, 새 아이폰 출시 일정 공개", "corp"),
        ("S&P 500 and Nasdaq close at record highs as tech rallies", "market"),
        ("뉴욕증시, 기술주 강세에 나스닥 상승", "market"),
    ],
)
def test_categorize_news_keyword_rules(headline: str, expected: str) -> None:
    assert categorize_news(headline) == expected


def test_earnings_wins_over_rate_when_both_appear() -> None:
    assert categorize_news("Bank earnings jump as interest rates stay high") == "earn"


def test_rate_word_inside_other_words_does_not_match() -> None:
    # corporate, accelerate 안의 "rate" 는 금리 신호가 아니다.
    assert categorize_news("Corporate spending accelerates in the cloud", symbols=["MSFT", "AMZN"]) == "market"


def test_summary_is_used_only_when_headline_has_no_signal() -> None:
    assert categorize_news("What investors should know today", "The Federal Reserve meets next week.") == "rate"
    assert categorize_news("Fed minutes released", "Nvidia earnings are due later.") == "rate"


def test_no_signal_defaults_to_corp_for_single_symbol_else_market() -> None:
    assert categorize_news("Nvidia shares move higher", symbols=["NVDA"]) == "corp"
    assert categorize_news("Two chip names to watch", symbols=["NVDA", "AMD"]) == "market"
    assert categorize_news("") == "market"


def test_categorize_is_deterministic_and_in_allowed_set() -> None:
    headline = "Oil slides as Europe demand weakens"
    assert categorize_news(headline) == categorize_news(headline)
    assert categorize_news(headline) in NEWS_CATEGORIES


def test_normalize_news_category() -> None:
    assert normalize_news_category(" EARN ") == "earn"
    assert normalize_news_category("") is None
    assert normalize_news_category("sports") is None


def test_ingest_event_and_clickhouse_rows_carry_category() -> None:
    article = {
        "id": 123,
        "headline": "Nvidia earnings beat estimates",
        "summary": "Data center revenue grew.",
        "symbols": ["NVDA", "AMD"],
        "created_at": "2026-10-30T12:00:00Z",
        "source": "benzinga",
    }
    event = build_news_event(article, symbol="NVDA", received_at="2026-10-30T12:00:01Z")
    assert event["category"] == "earn"

    assert news_to_clickhouse_row(event)["category"] == "earn"
    legacy_event = {key: value for key, value in event.items() if key != "category"}
    assert news_to_clickhouse_row(legacy_event)["category"] == "earn"

    record = build_news_intelligence_record(event, locale="ko-KR", localized_at="2026-10-30T12:00:02Z")
    assert record["category"] == "earn"
    assert news_intelligence_to_clickhouse_row(record)["category"] == "earn"


def test_category_migration_adds_columns_and_recreates_latest_views() -> None:
    migrations = ROOT / "systems" / "market-data" / "jobs" / "clickhouse-migrations" / "migrations"
    sql = (migrations / "0002_news_article_category.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE market_data.news_articles ADD COLUMN IF NOT EXISTS category LowCardinality(String) DEFAULT ''" in sql
    assert "ALTER TABLE market_data.news_article_localizations ADD COLUMN IF NOT EXISTS category" in sql
    assert "CREATE OR REPLACE VIEW market_data.news_articles_latest" in sql
    assert "CREATE OR REPLACE VIEW market_data.news_article_localizations_latest" in sql

    spec = importlib.util.spec_from_file_location("gops_clickhouse_migrations_0002", migrations.parent / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = module.split_statements(sql)
    assert len(statements) == 4

    init_sql = (ROOT / "infra" / "clickhouse" / "initdb" / "01-market-data.sql").read_text(encoding="utf-8")
    assert init_sql.count("category LowCardinality(String) DEFAULT ''") == 2


def test_provider_news_query_falls_back_when_category_column_is_missing() -> None:
    from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider

    queries: list[str] = []

    class Provider(ClickHouseMarketDataProvider):
        def __init__(self) -> None:
            self.database = "market_data"

        def query_json_each_row(self, query, parameters):
            queries.append(query)
            if "category" in query.split("FROM")[0]:
                raise RuntimeError("ClickHouse query failed: status=404, body=Code: 47. DB::Exception: Unknown expression identifier `category`. (UNKNOWN_IDENTIFIER)")
            return [{"articleId": "a1"}]

    provider = Provider()
    assert provider.localized_news_article_by_id("a1") == {"articleId": "a1"}
    assert provider.localized_news_article_by_id("a1") == {"articleId": "a1"}
    # 첫 실패 뒤에는 category 없이 곧바로 조회한다.
    assert len(queries) == 3
    assert "{category_select}" not in queries[-1]
