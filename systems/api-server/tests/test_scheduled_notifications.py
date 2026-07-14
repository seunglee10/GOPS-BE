from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from app.alerts.preferences import InMemoryNotificationPreferenceRepository
    from app.alerts.repository import InMemoryAlertRepository
    from app.alerts.scheduled import ClickHouseEarningsCalendar, ScheduledNotificationService
except Exception as exc:  # pragma: no cover - dependency guard for lean envs
    pytest.skip(f"scheduled notification tests are unavailable: {exc}", allow_module_level=True)


class Broker:
    def __init__(self):
        self.published = []

    def publish_user(self, user_sub, payload):
        self.published.append((user_sub, payload))


def test_market_close_sends_one_filtered_watchlist_summary_and_dedupes() -> None:
    preferences = InMemoryNotificationPreferenceRepository()
    preferences.patch(
        "user-a",
        settings={"marketClose": True},
        thresholds={},
        company_overrides={"AAPL": False},
    )
    notifications = InMemoryAlertRepository()
    broker = Broker()
    watchlists = {
        "user-a": [
            {"symbol": "AAPL", "name": "Apple", "lastPrice": 210, "changePercent": 1.2},
            {"symbol": "NVDA", "name": "NVIDIA", "lastPrice": 180, "changePercent": -0.8},
        ],
        "user-b": [{"symbol": "MSFT", "name": "Microsoft", "lastPrice": 500, "changePercent": 0.3}],
    }
    service = ScheduledNotificationService(
        preference_repository=preferences,
        notification_repository=notifications,
        broker=broker,
        user_provider=lambda: ["user-a", "user-b"],
        watchlist_provider=lambda user_sub: watchlists[user_sub],
        earnings_provider=lambda target: [],
    )
    now = datetime(2026, 7, 14, 21, 10, tzinfo=timezone.utc)

    first = service.send_market_close_summaries(now)
    second = service.send_market_close_summaries(now)

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second["duplicates"] == 1
    assert len(broker.published) == 1
    notification = next(iter(notifications.notifications.values()))
    assert notification["type"] == "system.market_close_summary"
    assert notification["payload"]["items"] == [{
        "symbol": "NVDA",
        "name": "NVIDIA",
        "lastPrice": 180.0,
        "changePercent": -0.8,
    }]


def test_market_close_skips_non_session_date() -> None:
    service = ScheduledNotificationService(
        preference_repository=InMemoryNotificationPreferenceRepository(),
        notification_repository=InMemoryAlertRepository(),
        broker=Broker(),
        user_provider=lambda: ["user-a"],
        watchlist_provider=lambda user_sub: [{"symbol": "AAPL", "changePercent": 1}],
        earnings_provider=lambda target: [],
    )

    result = service.send_market_close_summaries(datetime(2026, 7, 4, 21, 10, tzinfo=timezone.utc))

    assert result["sent"] == 0
    assert result["skipped"] == "market_closed"


def test_earnings_d1_uses_calendar_and_company_mute() -> None:
    preferences = InMemoryNotificationPreferenceRepository()
    preferences.patch(
        "user-a",
        settings={},
        thresholds={},
        company_overrides={"MSFT": False},
    )
    notifications = InMemoryAlertRepository()
    broker = Broker()
    service = ScheduledNotificationService(
        preference_repository=preferences,
        notification_repository=notifications,
        broker=broker,
        user_provider=lambda: ["user-a"],
        watchlist_provider=lambda user_sub: [
            {"symbol": "AAPL", "name": "Apple"},
            {"symbol": "MSFT", "name": "Microsoft"},
        ],
        earnings_provider=lambda target: [
            {"symbol": "AAPL", "source": "yahoo-earnings-dates"},
            {"symbol": "MSFT", "source": "yahoo-earnings-dates"},
        ] if target == date(2026, 7, 15) else [],
    )

    result = service.send_earnings_d1(datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc))

    assert result == {
        "job": "earnings-d1",
        "earningsDate": "2026-07-15",
        "events": 2,
        "sent": 1,
        "duplicates": 0,
    }
    notification = next(iter(notifications.notifications.values()))
    assert notification["type"] == "system.earnings_d1"
    assert notification["payload"]["symbol"] == "AAPL"
    assert broker.published[0][0] == "user-a"


def test_clickhouse_earnings_calendar_queries_only_calendar_rows() -> None:
    class Provider:
        def __init__(self):
            self.query = ""
            self.parameters = {}

        def table(self, name):
            return f"market_data.{name}"

        def query_json_each_row(self, query, parameters):
            self.query = query
            self.parameters = parameters
            return [{"symbol": "AAPL", "earningsDate": "2026-07-15"}]

    provider = Provider()
    rows = ClickHouseEarningsCalendar(provider).events_on(date(2026, 7, 15))

    assert rows[0]["symbol"] == "AAPL"
    assert "yahoo_earnings_estimates" in provider.query
    assert "sourceFrame" in provider.query
    assert provider.parameters == {"targetDate": "2026-07-15"}
