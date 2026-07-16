from datetime import datetime, timezone
from unittest import mock

import pytest
from fastapi import HTTPException

from app.market_data.query.service import MarketDataQueryService


class FakeClickHouseEventsProvider:
    def __init__(self, *, earnings=None, news=None):
        self.earnings = list(earnings or [])
        self.news = list(news or [])
        self.earnings_calls = []
        self.news_calls = []

    def earnings_events(self, symbol, from_time, to_time):
        self.earnings_calls.append((symbol, from_time, to_time))
        return self.earnings

    def company_daily_news_summaries_between(self, symbol, from_date, to_date, limit=370, locale="ko-KR"):
        self.news_calls.append((symbol, from_date, to_date, limit, locale))
        return self.news


class FakeEventsProvider:
    def __init__(self, clickhouse):
        self.clickhouse_provider = clickhouse


def make_service(clickhouse):
    return MarketDataQueryService(
        provider=FakeEventsProvider(clickhouse),
        backfill_service=object(),
        fill_service=object(),
        redis_client=object(),
        derived_service=object(),
    )


def test_chart_events_return_db_news_and_eps_events_without_external_calls():
    clickhouse = FakeClickHouseEventsProvider(
        earnings=[
            {
                "eventAt": "2026-07-15T20:05:00.000Z",
                "actualValue": 1.40,
                "estimate": 1.25,
                "surprisePercent": 12.0,
                "eventSession": "after",
                "eventStatus": "reported",
                "source": "yahoo-finance",
                "sourceAsOf": "2026-07-16T22:30:00.000Z",
            },
            {
                "eventAt": "2026-08-01T12:00:00.000Z",
                "actualValue": None,
                "estimate": 1.55,
                "surprisePercent": None,
                "eventSession": "pre",
                "eventStatus": "scheduled",
                "source": "yahoo-finance",
                "sourceAsOf": "2026-07-16T22:30:00.000Z",
            },
        ],
        news=[
            {
                "date": "2026-07-15",
                "summary": "반도체 수요 관련 뉴스입니다.",
                "keyPoints": ["데이터센터 수요"],
                "impactDirection": "positive",
                "sentiment": "positive",
                "articleIds": ["a", "b"],
                "articleCount": 2,
                "generatedAt": "2026-07-15T22:00:00.000Z",
                "sources": [
                    {"articleId": "a", "title": "AMD demand", "url": "https://example.com/a"},
                    {"articleId": "b", "title": "AMD launch", "url": "https://example.com/b"},
                ],
            },
            {
                "date": "2026-07-15",
                "summary": "더 최신인 일별 요약입니다.",
                "keyPoints": ["신제품 출시"],
                "impactDirection": "mixed",
                "sentiment": "neutral",
                "articleIds": ["b", "c"],
                "articleCount": 2,
                "generatedAt": "2026-07-15T23:00:00.000Z",
                "sources": [
                    {"articleId": "b", "title": "AMD launch", "url": "https://example.com/b"},
                    {"articleId": "c", "title": "AMD outlook", "url": "https://example.com/c"},
                ],
            },
        ],
    )
    service = make_service(clickhouse)

    with mock.patch("app.market_data.query.service.sp500_universe_symbols", return_value=["AMD"]):
        payload = service.chart_events(
            "amd",
            "2026-07-01T00:00:00Z",
            "2026-07-31T23:59:59Z",
            now=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
        )

    assert payload["status"] == {"earnings": "ready", "news": "ready"}
    assert payload["earnings"][0]["eps"] == {
        "actual": 1.4,
        "estimate": 1.25,
        "surprise": pytest.approx(0.15),
        "surprisePercent": 12.0,
    }
    assert payload["newsDays"][0]["summary"] == "더 최신인 일별 요약입니다."
    assert payload["newsDays"][0]["articleCount"] == 3
    assert len(payload["newsDays"][0]["sources"]) == 3
    assert payload["upcomingEarnings"]["eventAt"] == "2026-08-01T12:00:00.000Z"
    assert payload["upcomingEarnings"]["daysRemaining"] == 16
    assert len(clickhouse.earnings_calls) == 1
    assert clickhouse.news_calls == [("AMD", "2026-06-30", "2026-07-31", 370, "ko-KR")]


def test_chart_events_non_sp500_symbol_skips_earnings_but_keeps_news():
    clickhouse = FakeClickHouseEventsProvider(news=[{
        "date": "2026-07-15",
        "summary": "저장 뉴스",
        "articleIds": ["news-1"],
        "articleCount": 1,
        "sources": [{"articleId": "news-1", "title": "Stored", "url": "https://example.com/stored"}],
    }])
    service = make_service(clickhouse)

    with mock.patch("app.market_data.query.service.sp500_universe_symbols", return_value=["AMD"]):
        payload = service.chart_events("PLTR", "2026-07-01T00:00:00Z", "2026-07-31T23:59:59Z")

    assert payload["status"]["earnings"] == "empty"
    assert payload["earnings"] == []
    assert payload["newsDays"][0]["articleCount"] == 1
    assert clickhouse.earnings_calls == []


def test_chart_events_news_range_uses_new_york_market_dates():
    clickhouse = FakeClickHouseEventsProvider()
    service = make_service(clickhouse)

    with mock.patch("app.market_data.query.service.sp500_universe_symbols", return_value=[]):
        service.chart_events(
            "AMD",
            "2026-07-16T00:30:00Z",
            "2026-07-16T02:30:00Z",
        )

    assert clickhouse.news_calls == [("AMD", "2026-07-15", "2026-07-15", 370, "ko-KR")]


@pytest.mark.parametrize(
    ("from_time", "to_time", "locale"),
    [
        ("bad", "2026-07-31T23:59:59Z", "ko-KR"),
        ("2026-08-01T00:00:00Z", "2026-07-31T23:59:59Z", "ko-KR"),
        ("2026-07-01T00:00:00Z", "2026-07-31T23:59:59Z", "korean"),
    ],
)
def test_chart_events_validate_range_and_locale(from_time, to_time, locale):
    service = make_service(FakeClickHouseEventsProvider())

    with pytest.raises(HTTPException) as raised:
        service.chart_events("AMD", from_time, to_time, locale=locale)

    assert raised.value.status_code == 400


def test_chart_events_invalid_symbol_is_a_client_error():
    service = make_service(FakeClickHouseEventsProvider())

    with pytest.raises(HTTPException) as raised:
        service.chart_events("AAPL;DROP", "2026-07-01T00:00:00Z", "2026-07-31T23:59:59Z")

    assert raised.value.status_code == 400
