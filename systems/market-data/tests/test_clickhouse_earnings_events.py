from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))

from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider


class RecordingClickHouseProvider(ClickHouseMarketDataProvider):
    def __init__(self) -> None:
        super().__init__(
            url="http://clickhouse.local:8123",
            database="market_data",
            user="user",
            password="password",
        )
        self.query = ""
        self.parameters = {}

    def query_json_each_row(self, query, parameters):
        self.query = query
        self.parameters = parameters
        return []


def test_earnings_events_filters_on_datetime_before_formatting() -> None:
    provider = RecordingClickHouseProvider()

    assert provider.earnings_events(
        "AMD",
        "2025-07-01T00:00:00.000Z",
        "2026-10-14T00:00:00.000Z",
    ) == []

    assert "AS event_at_value" in provider.query
    assert "WHERE event_at_value >= parseDateTime64BestEffort" in provider.query
    assert "ORDER BY event_at_value ASC" in provider.query
    assert "WHERE eventAt >=" not in provider.query
    assert provider.parameters == {
        "symbol": "AMD",
        "fromTime": "2025-07-01T00:00:00.000Z",
        "toTime": "2026-10-14T00:00:00.000Z",
    }


def test_chart_news_events_limit_snapshots_to_the_replay_cursor() -> None:
    provider = RecordingClickHouseProvider()

    assert provider.company_daily_news_summaries_between(
        "AMD",
        "2026-07-01",
        "2026-07-31",
        as_of="2026-07-14T15:00:00.000Z",
    ) == []

    assert "generated_at <= parseDateTime64BestEffort({asOf:String})" in provider.query
    assert "FROM market_data.news_company_daily_summaries AS summaries" in provider.query
    assert "AND summaries.date >= toDate({fromDate:String})" in provider.query
    assert "GROUP BY summaries.date, summaries.symbol, summaries.locale" in provider.query
    assert provider.parameters["asOf"] == "2026-07-14T15:00:00.000Z"


def test_reconstructed_daily_news_requires_every_source_before_replay_cursor() -> None:
    provider = RecordingClickHouseProvider()

    assert provider.company_daily_news_summaries_reconstructed_between(
        "NVDA",
        "2026-06-14",
        "2026-07-14",
        as_of="2026-07-14T15:00:00.000Z",
    ) == []

    assert "summaries.version = 'v2'" in provider.query
    assert "arrayAll(" in provider.query
    assert "localizations.published_at <= parseDateTime64BestEffort({asOf:String})" in provider.query
    assert "localizations.target_symbol = {symbol:String}" in provider.query
    assert "'historical_reconstruction' AS sourceMode" in provider.query
    assert provider.parameters == {
        "symbol": "NVDA",
        "locale": "ko-KR",
        "fromDate": "2026-06-14",
        "toDate": "2026-07-14",
        "limit": 370,
        "asOf": "2026-07-14T15:00:00.000Z",
    }


def test_localized_news_as_of_excludes_articles_after_the_replay_cursor() -> None:
    provider = RecordingClickHouseProvider()

    assert provider.localized_news_articles_for_symbols_as_of(
        ["NVDA"],
        "2026-07-14T15:00:00.000Z",
        limit=30,
        days=30,
        locale="ko-KR",
    ) == []

    assert "published_at <= parseDateTime64BestEffort({asOf:String})" in provider.query
    assert "localized_at <= parseDateTime64BestEffort({asOf:String})" in provider.query
    assert "now64(3)" not in provider.query
    assert provider.parameters["symbols"] == ["NVDA"]
    assert provider.parameters["asOf"] == "2026-07-14T15:00:00.000Z"
    assert provider.parameters["days"] == 30
