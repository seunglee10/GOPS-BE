import io
import json
import sys
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems" / "market-data" / "shared"))

from market_data.economic_calendar import ingest
from market_data.economic_calendar.finnhub import FinnhubError, fetch_economic_calendar, format_number, normalize_events
from market_data.economic_calendar.items import build_earnings_items, econ_item, sort_items
from market_data.economic_calendar.titles import importance_from_impact, korean_title
from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider


FINNHUB_ROWS = [
    {"country": "US", "event": "CPI YoY", "impact": "high", "time": "2026-10-14 12:30:00", "actual": 2.9, "estimate": 3.0, "prev": 2.9, "unit": "%"},
    {"country": "US", "event": "Non Farm Payrolls", "impact": "high", "time": "2026-10-02 12:30:00", "actual": 221, "estimate": 180, "prev": 142, "unit": "K"},
    {"country": "US", "event": "Fed Interest Rate Decision", "impact": "high", "time": "2026-10-28 18:00:00", "actual": None, "estimate": 4.25, "prev": 4.5, "unit": "%"},
    {"country": "KR", "event": "BoK Interest Rate Decision", "impact": "medium", "time": "2026-10-22 01:00:00", "unit": "%"},
    {"country": "KR", "event": "Exports YoY", "impact": "low", "time": "2026-10-01 00:00:00", "actual": 12.7, "unit": "%"},
    {"country": "US", "event": "Some Obscure Index", "impact": "low", "time": "2026-10-05 14:00:00"},
    {"country": "JP", "event": "BoJ Interest Rate Decision", "impact": "high", "time": "2026-10-30 03:00:00"},
    {"country": "US", "event": "", "time": "2026-10-05 14:00:00"},
]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(payload, calls=None):
    def opener(request, timeout=None):
        if calls is not None:
            calls.append(request.full_url)
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return opener


# ---------- 한글 이름·중요도 ----------

def test_korean_titles_for_common_indicators() -> None:
    assert korean_title("CPI YoY", "us") == ("소비자물가(CPI) 전년 대비", "major")
    assert korean_title("Core CPI MoM", "us") == ("근원 소비자물가(CPI) 전월 대비", "major")
    assert korean_title("Non Farm Payrolls", "us") == ("비농업 고용", "major")
    assert korean_title("Fed Interest Rate Decision", "us") == ("FOMC 금리 결정", "major")
    assert korean_title("FOMC Minutes", "us") == ("FOMC 의사록 공개", "watch")
    assert korean_title("BoK Interest Rate Decision", "kr") == ("한국은행 기준금리 결정", "major")
    assert korean_title("Interest Rate Decision", "kr") == ("한국은행 기준금리 결정", "major")
    assert korean_title("GDP Growth Rate QoQ Adv", "us") == ("경제성장률(GDP) 전분기 대비 속보치", "major")
    assert korean_title("Core PCE Price Index MoM", "us") == ("근원 개인소비지출(PCE) 물가 전월 대비", "major")
    assert korean_title("Retail Sales MoM", "us")[0] == "소매판매 전월 대비"
    assert korean_title("ISM Manufacturing PMI", "us")[0] == "ISM 제조업 지수"
    assert korean_title("Unemployment Rate", "kr")[0] == "실업률"
    assert korean_title("Inflation Rate YoY", "kr")[0] == "소비자물가(CPI) 전년 대비"
    assert korean_title("Exports YoY", "kr") == ("수출 전년 대비", "watch")
    assert korean_title("Michigan Consumer Sentiment Final", "us")[0] == "미시간대 소비자심리지수 확정치"
    assert korean_title("Some Obscure Index", "us") == (None, "normal")


def test_importance_from_impact_respects_floor() -> None:
    assert importance_from_impact("high") == "major"
    assert importance_from_impact("medium") == "watch"
    assert importance_from_impact("low") == "normal"
    assert importance_from_impact(None) == "normal"
    assert importance_from_impact("low", "major") == "major"
    assert importance_from_impact("high", "watch") == "major"


def test_format_number() -> None:
    assert format_number(3.1) == "3.1"
    assert format_number(221.0) == "221"
    assert format_number(-0.25) == "-0.25"
    assert format_number(None) is None
    assert format_number(float("nan")) is None
    assert format_number(" 1.2 ") == "1.2"


# ---------- Finnhub 정규화 ----------

def test_normalize_keeps_us_kr_and_converts_to_seoul_time() -> None:
    events = {event.title_en: event for event in normalize_events(FINNHUB_ROWS)}
    assert set(events) == {"CPI YoY", "Non Farm Payrolls", "Fed Interest Rate Decision", "BoK Interest Rate Decision", "Exports YoY", "Some Obscure Index"}

    cpi = events["CPI YoY"]
    assert (cpi.event_date, cpi.event_time_local) == (date(2026, 10, 14), "21:30")
    assert (cpi.region, cpi.title_ko, cpi.importance) == ("us", "소비자물가(CPI) 전년 대비", "major")
    assert (cpi.actual, cpi.forecast, cpi.previous, cpi.unit) == ("2.9", "3", "2.9", "%")
    assert cpi.event_id.startswith("finnhub:us:20261014T1230:")

    fomc = events["Fed Interest Rate Decision"]
    # 18:00 UTC → 다음 날 03:00 KST
    assert (fomc.event_date, fomc.event_time_local, fomc.actual) == (date(2026, 10, 29), "03:00", None)

    bok = events["BoK Interest Rate Decision"]
    assert (bok.region, bok.event_time_local, bok.importance) == ("kr", "10:00", "major")

    obscure = events["Some Obscure Index"]
    assert obscure.title_ko is None and obscure.importance == "normal"


def test_event_id_is_stable_across_runs() -> None:
    first = normalize_events(FINNHUB_ROWS)
    second = normalize_events(list(reversed(FINNHUB_ROWS)))
    assert [e.event_id for e in first] == [e.event_id for e in second]


def test_fetch_builds_url_and_reads_list() -> None:
    calls: list[str] = []
    rows = fetch_economic_calendar(
        "secret", date(2026, 10, 1), date(2026, 11, 5),
        urlopen=fake_urlopen({"economicCalendar": FINNHUB_ROWS[:2]}, calls),
    )
    assert len(rows) == 2
    assert calls == ["https://finnhub.io/api/v1/calendar/economic?from=2026-10-01&to=2026-11-05&token=secret"]


def test_fetch_errors_raise_finnhub_error() -> None:
    def forbidden(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    for opener in (forbidden, fake_urlopen({"error": "nope"})):
        try:
            fetch_economic_calendar("k", date(2026, 10, 1), date(2026, 10, 2), urlopen=opener)
        except FinnhubError:
            continue
        raise AssertionError("expected FinnhubError")


# ---------- 잡 ----------

NOW = datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)  # 한국 시간 2026-10-19 12:00


def test_run_skips_without_api_key() -> None:
    def never(*args, **kwargs):
        raise AssertionError("must not be called")

    assert ingest.run(api_key=None, conninfo="postgresql://x", now=NOW, urlopen=never, upsert=never) == {
        "status": "skipped",
        "reason": "finnhub_api_key_missing",
    }


def test_main_exits_zero_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert ingest.main([]) == 0


def test_run_upserts_normalized_rows() -> None:
    calls: list[str] = []
    written: list[dict] = []

    def upsert(conninfo, rows):
        written.extend(rows)
        return len(rows)

    result = ingest.run(
        api_key="k", conninfo="postgresql://x", now=NOW, past_days=7, future_days=35,
        urlopen=fake_urlopen({"economicCalendar": FINNHUB_ROWS}, calls), upsert=upsert,
    )
    assert "from=2026-10-12&to=2026-11-23" in calls[0]
    assert result["status"] == "written"
    assert result["upserted"] == 6 == len(written)
    assert result["untranslated"] == 1
    assert {row["region"] for row in written} == {"us", "kr"}


def test_run_without_database_is_dry_run() -> None:
    result = ingest.run(api_key="k", conninfo=None, now=NOW, urlopen=fake_urlopen({"economicCalendar": FINNHUB_ROWS}))
    assert result["status"] == "dry_run" and result["events"] == 6


def test_job_entrypoint_exists() -> None:
    source = (REPO_ROOT / "systems" / "market-data" / "jobs" / "economic-calendar" / "main.py").read_text(encoding="utf-8")
    assert "market_data.economic_calendar.ingest" in source


# ---------- D28 항목 ----------

def test_econ_item_shape() -> None:
    item = econ_item({
        "event_id": "finnhub:us:x", "event_date": date(2026, 10, 14), "event_time_local": "21:30", "region": "us",
        "title_ko": "소비자물가(CPI) 전년 대비", "title_en": "CPI YoY", "importance": "major",
        "actual": "2.9", "forecast": "3", "previous": "2.9", "unit": "%",
    })
    assert item == {
        "id": "econ:finnhub:us:x", "date": "2026-10-14", "kind": "econ", "region": "us",
        "title": "소비자물가(CPI) 전년 대비", "level": "major", "at": "21:30", "done": True,
        "actual": "2.9%", "forecast": "3%", "previous": "2.9%",
    }
    fallback = econ_item({"event_id": "y", "event_date": "2026-10-05", "region": "us", "title_ko": None, "title_en": "Some Index", "importance": "normal"})
    assert (fallback["title"], fallback["level"], fallback["done"], fallback["actual"]) == ("Some Index", None, False, None)


def test_earnings_items_levels_and_call() -> None:
    rows = [
        {"symbol": "MSFT", "eventAt": "2026-10-28T20:00:00.000Z", "eventSession": "after", "eventStatus": "scheduled", "actualValue": None},
        {"symbol": "KO", "eventAt": "2026-10-27T11:00:00.000Z", "eventSession": "pre", "eventStatus": "scheduled"},
        {"symbol": "ZZZZ", "eventAt": "2026-10-27T11:00:00.000Z", "eventSession": "unknown", "eventStatus": "reported", "actualValue": 1.2},
        {"symbol": "XXXX", "eventAt": "2026-10-27T11:00:00.000Z"},
    ]
    names = {"MSFT": "마이크로소프트", "KO": "코카콜라"}
    items = build_earnings_items(
        rows, name_ko=names.get, personal={"MSFT"}, major={"MSFT", "KO"}, universe={"MSFT", "KO", "ZZZZ"},
        from_date=date(2026, 10, 1), to_date=date(2026, 10, 31),
    )
    by_symbol = {item["symbol"]: item for item in items}
    assert set(by_symbol) == {"MSFT", "KO", "ZZZZ"}
    assert by_symbol["MSFT"] == {
        "id": "earn:MSFT:2026-10-29", "date": "2026-10-29", "kind": "earn", "region": "us",
        "title": "마이크로소프트 실적", "symbol": "MSFT", "nameKo": "마이크로소프트", "call": "장 마감 후",
        "level": "watch", "done": False,
    }
    assert (by_symbol["KO"]["call"], by_symbol["KO"]["level"]) == ("장 시작 전", "major")
    assert (by_symbol["ZZZZ"]["title"], by_symbol["ZZZZ"]["call"], by_symbol["ZZZZ"]["level"], by_symbol["ZZZZ"]["done"]) == ("ZZZZ 실적", None, None, True)


def test_sort_items_by_date_then_time() -> None:
    items = [
        {"date": "2026-10-29", "at": None, "kind": "earn", "title": "b"},
        {"date": "2026-10-29", "at": "03:00", "kind": "econ", "title": "a"},
        {"date": "2026-10-14", "at": "21:30", "kind": "econ", "title": "c"},
    ]
    assert [item["title"] for item in sort_items(items)] == ["c", "a", "b"]


class RecordingClickHouseProvider(ClickHouseMarketDataProvider):
    def __init__(self) -> None:
        super().__init__(url="http://clickhouse.local:8123", database="market_data", user="u", password="p")
        self.query = ""
        self.parameters = {}

    def query_json_each_row(self, query, parameters):
        self.query = query
        self.parameters = parameters
        return []


def test_earnings_events_between_queries_all_symbols_in_range() -> None:
    provider = RecordingClickHouseProvider()
    assert provider.earnings_events_between("2026-10-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z") == []
    assert "FROM market_data.yahoo_earnings_estimates" in provider.query
    assert "WHERE symbol" not in provider.query
    assert "metric = 'eps'" in provider.query
    assert "WHERE event_at_value >= parseDateTime64BestEffort({fromTime:String})" in provider.query
    assert provider.parameters == {"fromTime": "2026-10-01T00:00:00.000Z", "toTime": "2026-11-01T00:00:00.000Z"}
