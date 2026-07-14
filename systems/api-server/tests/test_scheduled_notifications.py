from __future__ import annotations

import sys
from datetime import datetime, timezone
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
    from app.alerts.scheduled import ScheduledNotificationService
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
    preferences.patch(
        "user-b",
        settings={"marketClose": False},
        thresholds={},
        company_overrides={},
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
    )

    result = service.send_market_close_summaries(datetime(2026, 7, 4, 21, 10, tzinfo=timezone.utc))

    assert result["sent"] == 0
    assert result["skipped"] == "market_closed"


def test_market_open_reminder_sends_at_0920_new_york_and_dedupes() -> None:
    preferences = InMemoryNotificationPreferenceRepository()
    notifications = InMemoryAlertRepository()
    broker = Broker()
    service = ScheduledNotificationService(
        preference_repository=preferences,
        notification_repository=notifications,
        broker=broker,
        user_provider=lambda: ["user-a"],
        watchlist_provider=lambda _user_sub: [],
    )
    now = datetime(2026, 7, 14, 13, 20, tzinfo=timezone.utc)

    first = service.send_market_reminders(now)
    second = service.send_market_reminders(now)

    assert first["kind"] == "market_open"
    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second["duplicates"] == 1
    assert broker.published[0][1]["notification"]["type"] == "system.market_open"
