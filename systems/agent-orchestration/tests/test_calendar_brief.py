from __future__ import annotations

import json
from datetime import date, datetime, timezone

from gops_agents.summaries import CALENDAR_BRIEF_TTL_SECONDS, CalendarBriefGenerator, CalendarBriefInput, calendar_brief_key
from gops_agents.summaries.calendar_brief import brief_facts, current_and_next_week, template_text, week_bounds
from gops_agents.summaries.llm import SummaryModelError
from gops_agents.summaries.refresh import refresh_calendar_briefs
from gops_agents.summaries.text_rules import is_haeyo

NOW = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)  # 한국 시간 2026-10-26(월) 01:00

ITEMS = [
    {"date": "2026-10-29", "kind": "econ", "region": "us", "title": "FOMC 금리 결정", "level": "major", "at": "03:00"},
    {"date": "2026-10-30", "kind": "econ", "region": "us", "title": "근원 개인소비지출(PCE) 물가 전월 대비", "level": "major", "at": "21:30"},
    {"date": "2026-10-30", "kind": "econ", "region": "us", "title": "근원 개인소비지출(PCE) 물가 전년 대비", "level": "major", "at": "21:30"},
    {"date": "2026-10-27", "kind": "econ", "region": "us", "title": "신규주택 매매건수", "level": None, "at": "23:00"},
    {"date": "2026-10-29", "kind": "earn", "region": "us", "title": "마이크로소프트 실적", "symbol": "MSFT", "nameKo": "마이크로소프트", "level": "major"},
    {"date": "2026-10-31", "kind": "earn", "region": "us", "title": "애플 실적", "symbol": "AAPL", "nameKo": "애플", "level": "major"},
]


class FakeModel:
    model = "fake"

    def __init__(self, output=None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.output


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex


class FakeSource:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def calendar_items(self, from_date, to_date):
        self.calls.append((from_date, to_date))
        return self.items


def test_template_lists_major_events_without_forecasts():
    text = template_text(brief_facts(CalendarBriefInput(week="2026-W44", items=ITEMS)))
    assert text == "이번 주에는 29일 FOMC 금리 결정, 30일 근원 개인소비지출(PCE) 물가 발표가 있고, 마이크로소프트, 애플 실적 발표도 이어져요."
    assert is_haeyo(text)
    assert "예상" not in text and "%" not in text


def test_template_without_major_uses_first_events():
    items = [{"date": "2026-10-27", "kind": "econ", "title": "신규주택 매매건수", "level": None}]
    text = template_text(brief_facts(CalendarBriefInput(week="2026-W44", items=items)))
    assert text == "이번 주에는 큰 일정이 적고, 27일 신규주택 매매건수 발표가 있어요."


def test_llm_output_is_used_when_valid():
    model = FakeModel({"text": "29일 FOMC 금리 결정과 마이크로소프트 실적 발표가 이어져요."})
    brief, used = CalendarBriefGenerator(model, now=lambda: NOW).generate_with_source(CalendarBriefInput(week="2026-W44", items=ITEMS))
    assert used == "llm"
    assert brief == {"week": "2026-W44", "generatedAt": "2026-10-25T16:00:00Z", "text": "29일 FOMC 금리 결정과 마이크로소프트 실적 발표가 이어져요."}
    payload = model.calls[0]["payload"]
    assert [event["title"] for event in payload["events"]] == ["FOMC 금리 결정", "마이크로소프트", "근원 개인소비지출(PCE) 물가", "애플"]


def test_llm_forecast_or_error_falls_back_to_template():
    for model in (
        FakeModel({"text": "FOMC 금리 결정에서 0.25% 인하가 예상돼요."}),
        FakeModel({"text": "이번 주는 조용해요."}),  # 입력 일정을 하나도 말하지 않음
        FakeModel({"text": "FOMC 금리 결정이 있다."}),  # 해요체 아님
        FakeModel(error=SummaryModelError("down", code="provider_http")),
    ):
        brief, used = CalendarBriefGenerator(model).generate_with_source(CalendarBriefInput(week="2026-W44", items=ITEMS))
        assert used == "template"
        assert brief["text"].startswith("이번 주에는 29일 FOMC 금리 결정")


def test_weeks_follow_seoul_time():
    assert current_and_next_week(NOW) == ["2026-W44", "2026-W45"]
    assert week_bounds("2026-W44") == (date(2026, 10, 26), date(2026, 11, 1))


def test_refresh_writes_both_weeks_with_ttl_and_throttles():
    redis = FakeRedis()
    source = FakeSource(ITEMS)
    generator = CalendarBriefGenerator(None, now=lambda: NOW)
    result = refresh_calendar_briefs(source, redis, generator, now=NOW)
    assert result == {"2026-W44": {"status": "written", "generator": "template"}, "2026-W45": {"status": "written", "generator": "template"}}
    key = calendar_brief_key("2026-W44")
    assert key == "gops:calendar-brief:2026-W44"
    assert redis.ttls[key] == CALENDAR_BRIEF_TTL_SECONDS == 8 * 24 * 3600
    assert json.loads(redis.values[key])["week"] == "2026-W44"
    assert source.calls[0] == (date(2026, 10, 26), date(2026, 11, 1))

    again = refresh_calendar_briefs(source, redis, generator, now=NOW)
    assert again["2026-W44"] == {"status": "skipped", "reason": "recently_generated"}
    forced = refresh_calendar_briefs(source, redis, generator, now=NOW, force=True)
    assert forced["2026-W44"]["status"] == "written"


def test_refresh_skips_empty_week_and_survives_source_errors():
    redis = FakeRedis()
    assert refresh_calendar_briefs(FakeSource([]), redis, CalendarBriefGenerator(None), now=NOW, weeks=["2026-W44"]) == {
        "2026-W44": {"status": "skipped", "reason": "no_events"}
    }

    class Broken:
        def calendar_items(self, *args):
            raise RuntimeError("db down")

    result = refresh_calendar_briefs(Broken(), redis, CalendarBriefGenerator(None), now=NOW, weeks=["2026-W44"])
    assert result["2026-W44"]["status"] == "failed"
    assert redis.values == {}


def test_store_source_calendar_items_reads_top_market_cap_earnings(monkeypatch):
    from gops_agents.summaries.sources import StoreSummarySource

    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    class FakeClickHouse:
        def __init__(self):
            self.calls = []

        def earnings_events_between(self, start, end):
            self.calls.append((start, end))
            return [
                {"symbol": "MSFT", "eventAt": "2026-10-28T20:05:00.000Z", "eventSession": "after"},
                {"symbol": "CLX", "eventAt": "2026-10-28T20:05:00.000Z", "eventSession": "after"},
            ]

    clickhouse = FakeClickHouse()
    source = StoreSummarySource(clickhouse=clickhouse, redis_client=None)
    monkeypatch.setattr(source, "heatmap_items", lambda: [{"symbol": "MSFT", "marketCap": 3e12}, {"symbol": "AAPL", "marketCap": 2e12}])
    items = source.calendar_items(date(2026, 10, 26), date(2026, 11, 1))
    assert clickhouse.calls == [("2026-10-25T15:00:00.000Z", "2026-11-01T15:00:00.000Z")]
    assert [(item["symbol"], item["level"], item["date"]) for item in items] == [("MSFT", "major", "2026-10-29")]
