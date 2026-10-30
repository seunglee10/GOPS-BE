import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (
    ROOT / "systems" / "market-data" / "shared",
    ROOT / "systems" / "order" / "shared",
    ROOT / "systems" / "agent-orchestration" / "shared",
    ROOT / "systems" / "api-server" / "pods" / "api-server",
    ROOT,
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from fastapi.testclient import TestClient

    from app.auth.dependencies import optional_current_user
    from app.auth.models import AuthenticatedUser
    from app.calendar.repository import InMemoryEconomicEventRepository
    from app.main import create_app
    from app.services.simulation_guard import requires_point_in_time_data

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


ECON_ROWS = [
    {"event_id": "finnhub:us:cpi", "event_date": date(2026, 10, 14), "event_time_local": "21:30", "region": "us",
     "title_ko": "소비자물가(CPI) 전년 대비", "title_en": "CPI YoY", "importance": "major",
     "actual": "2.9", "forecast": "3", "previous": "2.9", "unit": "%"},
    {"event_id": "finnhub:kr:bok", "event_date": date(2026, 10, 22), "event_time_local": "10:00", "region": "kr",
     "title_ko": "한국은행 기준금리 결정", "title_en": "BoK Interest Rate Decision", "importance": "major",
     "actual": None, "forecast": None, "previous": "2.5", "unit": "%"},
    {"event_id": "finnhub:us:fomc", "event_date": date(2026, 10, 29), "event_time_local": "03:00", "region": "us",
     "title_ko": "FOMC 금리 결정", "title_en": "Fed Interest Rate Decision", "importance": "major",
     "actual": None, "forecast": "4.25", "previous": "4.5", "unit": "%"},
    {"event_id": "finnhub:us:other", "event_date": date(2026, 11, 20), "event_time_local": None, "region": "us",
     "title_ko": None, "title_en": "Some Index", "importance": "normal"},
]

EARNINGS_ROWS = [
    {"symbol": "MSFT", "eventAt": "2026-10-28T20:05:00.000Z", "eventSession": "after", "eventStatus": "scheduled"},
    {"symbol": "KO", "eventAt": "2026-10-27T10:55:00.000Z", "eventSession": "pre", "eventStatus": "scheduled"},
    {"symbol": "CLX", "eventAt": "2026-10-29T20:15:00.000Z", "eventSession": "after", "eventStatus": "scheduled"},
    {"symbol": "TSM", "eventAt": "2026-10-15T06:00:00.000Z", "eventSession": "pre", "eventStatus": "reported", "actualValue": 2.1},
]


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)


def user(sub="google-sub-1"):
    return AuthenticatedUser(sub, "u@example.com", True, "U", None, "11111111-1111-1111-1111-111111111111")


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "fastapi TestClient unavailable")
class CalendarRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.state.economic_event_repository = InMemoryEconomicEventRepository(ECON_ROWS)
        self.earnings_calls = []

        def earnings(from_time, to_time):
            self.earnings_calls.append((from_time, to_time))
            return EARNINGS_ROWS

        self.app.state.calendar_earnings_provider = earnings
        self.app.state.calendar_universe_provider = lambda: {"MSFT", "KO", "CLX"}
        self.app.state.calendar_major_symbols_provider = lambda: {"MSFT", "KO"}
        self.app.state.calendar_user_symbols_provider = lambda u: {"clx"}
        self.redis = FakeRedis()
        self.app.state.summary_cache_redis = self.redis
        self.app.state.calendar_clock = lambda: datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc)
        self.client = TestClient(self.app)

    def get(self, **params):
        return self.client.get("/api/calendar", params=params)

    def test_returns_sorted_econ_and_earnings_items(self):
        response = self.get(**{"from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(
            [(item["date"], item["kind"], item["title"]) for item in items],
            [
                ("2026-10-14", "econ", "소비자물가(CPI) 전년 대비"),
                ("2026-10-22", "econ", "한국은행 기준금리 결정"),
                ("2026-10-27", "earn", "코카콜라 실적"),
                ("2026-10-29", "econ", "FOMC 금리 결정"),
                ("2026-10-29", "earn", "마이크로소프트 실적"),
                ("2026-10-30", "earn", "클로록스 실적"),
            ],
        )
        cpi = items[0]
        self.assertEqual(
            {k: cpi[k] for k in ("region", "level", "at", "done", "actual", "forecast", "previous")},
            {"region": "us", "level": "major", "at": "21:30", "done": True, "actual": "2.9%", "forecast": "3%", "previous": "2.9%"},
        )
        msft = items[4]
        self.assertEqual((msft["symbol"], msft["nameKo"], msft["call"], msft["region"]), ("MSFT", "마이크로소프트", "장 마감 후", "us"))
        # 한국 날짜 10/01~10/31 → UTC 9/30 15:00 ~ 10/31 15:00
        self.assertEqual(self.earnings_calls, [("2026-09-30T15:00:00.000Z", "2026-10-31T15:00:00.000Z")])

    def test_anonymous_marks_only_top_market_cap_major(self):
        items = self.get(**{"from": "2026-10-01", "to": "2026-10-31", "kind": "earn"}).json()["items"]
        levels = {item["symbol"]: item["level"] for item in items}
        self.assertEqual(levels, {"KO": "major", "MSFT": "major", "CLX": None})

    def test_signed_in_user_watchlist_and_holdings_are_watch(self):
        self.app.dependency_overrides[optional_current_user] = lambda: user()
        items = self.get(**{"from": "2026-10-01", "to": "2026-10-31", "kind": "earn"}).json()["items"]
        levels = {item["symbol"]: item["level"] for item in items}
        self.assertEqual(levels, {"KO": "major", "MSFT": "major", "CLX": "watch"})

    def test_kind_and_region_filters(self):
        econ = self.get(**{"from": "2026-10-01", "to": "2026-10-31", "kind": "econ"}).json()["items"]
        self.assertEqual({item["kind"] for item in econ}, {"econ"})
        self.assertEqual(len(econ), 3)
        kr = self.get(**{"from": "2026-10-01", "to": "2026-10-31", "region": "kr"}).json()["items"]
        self.assertEqual([item["title"] for item in kr], ["한국은행 기준금리 결정"])
        self.assertEqual(self.earnings_calls, [], "region=kr 는 실적을 읽지 않는다")
        us_earn = self.get(**{"from": "2026-10-01", "to": "2026-10-31", "region": "us", "kind": "earn"}).json()["items"]
        self.assertEqual(len(us_earn), 3)

    def test_unmapped_title_falls_back_to_english(self):
        items = self.get(**{"from": "2026-11-01", "to": "2026-11-30", "kind": "econ"}).json()["items"]
        self.assertEqual([(item["title"], item["level"], item["at"], item["done"]) for item in items], [("Some Index", None, None, False)])

    def test_range_validation(self):
        too_large = self.get(**{"from": "2026-10-01", "to": "2026-12-02"})
        self.assertEqual(too_large.status_code, 422)
        self.assertEqual(too_large.json()["detail"]["code"], "range_too_large")
        self.assertEqual(self.get(**{"from": "2026-10-01", "to": "2026-12-01"}).status_code, 200)  # 62일
        reversed_range = self.get(**{"from": "2026-10-10", "to": "2026-10-01"})
        self.assertEqual(reversed_range.json()["detail"]["code"], "invalid_range")
        bad = self.get(**{"from": "2026-13-01", "to": "2026-10-01"})
        self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (422, "invalid_date"))
        self.assertEqual(self.get(**{"from": "2026-10-01", "to": "2026-10-31", "kind": "fx"}).status_code, 422)
        self.assertEqual(self.client.get("/api/calendar").status_code, 422)

    def test_earnings_failure_still_returns_econ(self):
        def boom(from_time, to_time):
            raise RuntimeError("clickhouse down")

        self.app.state.calendar_earnings_provider = boom
        items = self.get(**{"from": "2026-10-01", "to": "2026-10-31"}).json()["items"]
        self.assertEqual({item["kind"] for item in items}, {"econ"})

    def test_econ_repository_failure_is_503(self):
        class Broken:
            def between(self, *args):
                raise RuntimeError("db down")

        self.app.state.economic_event_repository = Broken()
        response = self.get(**{"from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (503, "calendar_unavailable"))

    def test_brief(self):
        self.redis.values["gops:calendar-brief:2026-W44"] = json.dumps(
            {"week": "2026-W44", "generatedAt": "2026-10-26T00:00:00Z", "text": "이번 주에는 29일 FOMC 금리 결정 발표가 있어요."},
            ensure_ascii=False,
        )
        response = self.client.get("/api/calendar/brief", params={"week": "2026-W44"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "이번 주에는 29일 FOMC 금리 결정 발표가 있어요.")
        self.assertEqual(response.json()["week"], "2026-W44")
        # week 를 빼면 한국 시간 이번 주(2026-10-30 → W44)
        self.assertEqual(self.client.get("/api/calendar/brief").json()["week"], "2026-W44")

    def test_brief_not_ready_and_invalid_week(self):
        missing = self.client.get("/api/calendar/brief", params={"week": "2026-W45"})
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "brief_not_ready"))
        for week in ("2026-44", "2026-W54", "abc"):
            response = self.client.get("/api/calendar/brief", params={"week": week})
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (422, "invalid_week"), week)

    def test_calendar_is_blocked_in_simulation(self):
        self.assertTrue(requires_point_in_time_data("/api/calendar"))
        self.assertTrue(requires_point_in_time_data("/api/calendar/brief"))


if __name__ == "__main__":
    unittest.main()
