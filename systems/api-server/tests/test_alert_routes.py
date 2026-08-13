from __future__ import annotations

import os
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(AGENT_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

sys.modules.setdefault(
    "redis",
    types.SimpleNamespace(
        from_url=lambda *args, **kwargs: None,
        exceptions=types.SimpleNamespace(TimeoutError=TimeoutError),
    ),
)

try:
    from fastapi.testclient import TestClient

    from app.alerts.notifications import InMemoryNotificationBroker
    from app.alerts import routes as alert_routes
    from app.alerts.preferences import InMemoryNotificationPreferenceRepository
    from app.alerts.repository import AlertCreate, InMemoryAlertRepository, PostgresAlertRepository
    from app.auth.config import AuthConfig
    from app.auth.models import AuthenticatedUser
    from app.auth.session_store import MemorySessionStore
    from app.main import create_app
except Exception as exc:  # pragma: no cover - dependency guard for lean envs
    pytest.skip(f"FastAPI alert route tests are unavailable: {exc}", allow_module_level=True)


class FakeProjection:
    def __init__(self) -> None:
        self.upserted: list[dict] = []
        self.deleted: list[tuple[int, str | None]] = []

    def upsert_alert(self, alert: dict) -> None:
        self.upserted.append(alert)

    def delete_alert(self, alert_id: int, *, symbol: str | None = None) -> None:
        self.deleted.append((alert_id, symbol))


@pytest.fixture
def alert_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    app = create_app()
    app.state.alert_repository = InMemoryAlertRepository()
    app.state.alert_projection = FakeProjection()
    app.state.alert_notification_broker = InMemoryNotificationBroker()
    app.state.notification_preferences_repository = InMemoryNotificationPreferenceRepository()
    app.state.alert_price_provider = lambda symbol: {"symbol": symbol, "price": Decimal("100")}
    return app


def test_create_price_cross_alert_derives_direction_and_syncs_projection(alert_app) -> None:
    response = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "nvda", "type": "price_cross", "targetPrice": "110"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["projectionStatus"] == "synced"
    assert payload["alert"]["symbol"] == "NVDA"
    assert payload["alert"]["direction"] == "above"
    assert payload["alert"]["repeat_limit"] == 1
    assert payload["alert"]["triggered_count"] == 0
    assert alert_app.state.alert_projection.upserted[0]["target_price"] == 110.0


def test_one_shot_alert_leaves_active_list_after_notification_is_persisted(alert_app) -> None:
    client = TestClient(alert_app)
    created = client.post(
        "/api/alerts",
        json={"symbol": "NVDA", "type": "price_cross", "targetPrice": "110"},
    ).json()["alert"]

    notification, updated = alert_app.state.alert_repository.persist_triggered_notification(
        user_sub="dev-auth-disabled",
        alert_id=created["id"],
        event_id="one-shot-event",
        notification_type="alert.price_cross",
        payload={"alertId": created["id"], "symbol": "NVDA"},
    )

    assert notification is not None
    assert updated["status"] == "fired"
    assert client.get("/api/alerts?includeTerminal=false").json()["alerts"] == []


def test_create_alert_accepts_repeat_limit_options(alert_app) -> None:
    limited = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "nvda", "type": "price_cross", "targetPrice": "110", "repeatLimit": 3},
    )
    unlimited = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "aapl", "type": "price_cross", "targetPrice": "95", "repeatLimit": None},
    )

    assert limited.status_code == 201
    assert limited.json()["alert"]["repeat"] is True
    assert limited.json()["alert"]["repeat_limit"] == 3
    assert unlimited.status_code == 201
    assert unlimited.json()["alert"]["repeat"] is True
    assert unlimited.json()["alert"]["repeat_limit"] is None


def test_create_alert_preserves_ai_coach_proposal_source(alert_app) -> None:
    client = TestClient(alert_app)
    created = client.post(
        "/api/alerts",
        json={"symbol": "nvda", "type": "price_cross", "targetPrice": "110", "proposalSource": "daily_trade"},
    )

    assert created.status_code == 201
    assert created.json()["alert"]["proposal_source"] == "daily_trade"
    assert alert_app.state.alert_projection.upserted[0]["proposal_source"] == "daily_trade"
    assert client.get("/api/alerts").json()["alerts"][0]["proposal_source"] == "daily_trade"

    legacy = client.post(
        "/api/alerts",
        json={"symbol": "aapl", "type": "price_cross", "targetPrice": "90"},
    )
    assert legacy.status_code == 201
    assert legacy.json()["alert"]["proposal_source"] is None

    invalid = client.post(
        "/api/alerts",
        json={"symbol": "msft", "type": "price_cross", "targetPrice": "120", "proposalSource": "made_up"},
    )
    assert invalid.status_code == 422


def test_postgres_alert_insert_includes_proposal_source(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.query = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            self.query = query
            self.params = tuple(params)
            return self

        def fetchone(self):
            return {"id": 1, "proposal_source": "entry_habit"}

        def commit(self):
            return None

    connection = FakeConnection()
    repository = PostgresAlertRepository("postgresql://unused")
    monkeypatch.setattr(repository, "_connect", lambda: connection)

    created = repository.create_alert(AlertCreate(
        user_sub="user-1",
        symbol="NVDA",
        type="price_cross",
        direction="above",
        target_price=Decimal("110"),
        proposal_source="entry_habit",
    ))

    assert "proposal_source" in connection.query
    assert connection.params[11] == "entry_habit"
    assert created["proposal_source"] == "entry_habit"


def test_create_alert_rejects_unknown_repeat_limit(alert_app) -> None:
    response = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "NVDA", "type": "price_cross", "targetPrice": "110", "repeatLimit": 2},
    )

    assert response.status_code == 422


def test_create_rsi_condition_and_agent_command_are_persisted_with_source(alert_app) -> None:
    client = TestClient(alert_app)
    manual = client.post(
        "/api/alerts",
        headers={"Idempotency-Key": "manual-rsi-1"},
        json={
            "symbol": "NVDA",
            "condition": {
                "kind": "rsi_threshold",
                "operator": "above",
                "threshold": 70,
                "interval": "1D",
                "period": 14,
            },
            "repeatLimit": None,
        },
    )
    command = client.post(
        "/api/alerts/commands",
        headers={"Idempotency-Key": "agent-rsi-1"},
        json={"text": "NVDA RSI 70 이상이면 알림 설정해줘"},
    )
    replay = client.post(
        "/api/alerts/commands",
        headers={"Idempotency-Key": "agent-rsi-1"},
        json={"text": "다른 요청"},
    )

    assert manual.status_code == 201
    assert manual.json()["alert"]["condition"] == {
        "kind": "rsi_threshold",
        "operator": "above",
        "threshold": 70.0,
        "interval": "1D",
        "period": 14,
    }
    assert command.status_code == 200
    assert command.json()["status"] == "created"
    assert command.json()["alert"]["created_via"] == "agent_chat"
    assert command.json()["alert"]["repeat_limit"] == 1
    assert replay.json()["idempotentReplay"] is True
    assert replay.json()["alert"]["id"] == command.json()["alert"]["id"]


def test_agent_command_clarifies_missing_volume_interval_then_creates(alert_app) -> None:
    client = TestClient(alert_app)
    first = client.post(
        "/api/alerts/commands",
        headers={"Idempotency-Key": "agent-volume-1"},
        json={"text": "NVDA 거래량 1000000 이상이면 알림 설정해줘"},
    )

    assert first.status_code == 200
    assert first.json()["status"] == "clarify"
    assert first.json()["clarificationId"]

    second = client.post(
        "/api/alerts/commands",
        headers={"Idempotency-Key": "agent-volume-1"},
        json={"text": "5분봉", "clarificationId": first.json()["clarificationId"]},
    )

    assert second.status_code == 200
    assert second.json()["status"] == "created"
    assert second.json()["alert"]["condition"]["kind"] == "volume_absolute"
    assert second.json()["alert"]["condition"]["interval"] == "5m"


def test_agent_command_requires_idempotency_key(alert_app) -> None:
    response = TestClient(alert_app).post(
        "/api/alerts/commands",
        json={"text": "NVDA RSI 70 이상이면 알림 설정해줘"},
    )

    assert response.status_code == 400


def test_agent_command_uses_strict_fallback_for_unfamiliar_expression(alert_app, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(alert_routes, "request_agent_alert_resolution", lambda payload: calls.append(payload) or {
        "status": "ready",
        "symbol": "NVDA",
        "condition": {
            "kind": "volume_relative",
            "operator": "above",
            "threshold": 2,
            "interval": "5m",
            "lookback": 20,
        },
        "repeatLimit": 1,
    })

    response = TestClient(alert_app).post(
        "/api/alerts/commands",
        headers={"Idempotency-Key": "agent-fallback-1"},
        json={"text": "NVDA 거래량이 평소보다 크게 튀면 5분봉 알림 설정해줘"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["alert"]["condition"]["kind"] == "volume_relative"
    assert calls and calls[0]["contextSymbol"] == "NVDA"


def test_create_price_cross_rejects_equal_current_price(alert_app) -> None:
    response = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "NVDA", "type": "price_cross", "targetPrice": "100"},
    )

    assert response.status_code == 422
    assert alert_app.state.alert_repository.alerts == {}


def test_create_price_cross_reports_missing_current_price_in_korean(alert_app) -> None:
    alert_app.state.alert_price_provider = lambda symbol: {"symbol": symbol, "price": None}

    response = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "NVDA", "type": "price_cross", "targetPrice": "110"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "NVDA 현재가를 확인할 수 없어 목표가 알림을 등록하지 못했습니다. 잠시 후 다시 시도해주세요."
    assert alert_app.state.alert_repository.alerts == {}


def test_active_alert_limit_is_enforced(alert_app) -> None:
    repo = alert_app.state.alert_repository
    for index in range(50):
        repo.create_alert(
            AlertCreate(
                user_sub="dev-auth-disabled",
                symbol=f"T{index}",
                type="price_cross",
                direction="above",
                target_price=Decimal("10"),
            )
        )

    response = TestClient(alert_app).post(
        "/api/alerts",
        json={"symbol": "NVDA", "type": "price_cross", "targetPrice": "110"},
    )

    assert response.status_code == 409


def test_alert_and_notification_reads_are_scoped_by_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)

    app = create_app()
    config = AuthConfig.from_env()
    store = MemorySessionStore(config)
    repo = InMemoryAlertRepository()
    app.state.auth_session_store = store
    app.state.alert_repository = repo
    app.state.alert_projection = FakeProjection()
    app.state.alert_price_provider = lambda symbol: {"symbol": symbol, "price": Decimal("100")}
    client = TestClient(app)

    client.cookies.set(config.session_cookie_name, store.create_session(AuthenticatedUser("user-1", "one@test.local", True)))
    created = client.post("/api/alerts", json={"symbol": "AAPL", "type": "price_cross", "targetPrice": "101"})
    assert created.status_code == 201
    user_one_notification = repo.create_notification(
        user_sub="user-1",
        alert_id=created.json()["alert"]["id"],
        event_id="event-1",
        notification_type="alert.price_cross",
        payload={"ok": True},
    )

    client.cookies.set(config.session_cookie_name, store.create_session(AuthenticatedUser("user-2", "two@test.local", True)))
    assert client.get("/api/alerts").json()["alerts"] == []
    assert client.get("/api/notifications").json()["notifications"] == []
    assert client.patch("/api/notifications/read-all").json() == {"updated": 0}
    assert repo.notifications[user_one_notification["id"]]["read_at"] is None


def test_read_notification_can_be_deleted(alert_app) -> None:
    repo = alert_app.state.alert_repository
    client = TestClient(alert_app)
    notification = repo.create_notification(
        user_sub="dev-auth-disabled",
        alert_id=None,
        event_id="event-delete",
        notification_type="alert.price_cross",
        payload={"symbol": "NVDA", "targetPrice": 100, "direction": "above"},
    )

    unread_delete = client.delete(f"/api/notifications/{notification['id']}")
    assert unread_delete.status_code == 404

    read = client.patch(f"/api/notifications/{notification['id']}/read")
    assert read.status_code == 200

    deleted = client.delete(f"/api/notifications/{notification['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/api/notifications").json()["notifications"] == []


def test_mark_all_notifications_read_updates_only_unread_rows(alert_app) -> None:
    repo = alert_app.state.alert_repository
    client = TestClient(alert_app)
    first = repo.create_notification(
        user_sub="dev-auth-disabled",
        alert_id=None,
        event_id="event-read-all-1",
        notification_type="alert.price_cross",
        payload={"symbol": "NVDA"},
    )
    second = repo.create_notification(
        user_sub="dev-auth-disabled",
        alert_id=None,
        event_id="event-read-all-2",
        notification_type="alert.price_cross",
        payload={"symbol": "AAPL"},
    )
    already_read = repo.create_notification(
        user_sub="dev-auth-disabled",
        alert_id=None,
        event_id="event-read-all-3",
        notification_type="alert.price_cross",
        payload={"symbol": "MSFT"},
    )
    repo.mark_notification_read("dev-auth-disabled", already_read["id"])

    response = client.patch("/api/notifications/read-all")

    assert response.status_code == 200
    assert response.json() == {"updated": 2}
    payload = client.get("/api/notifications").json()
    assert payload["unreadCount"] == 0
    rows = {item["id"]: item for item in payload["notifications"]}
    assert rows[first["id"]]["read_at"] is not None
    assert rows[second["id"]]["read_at"] is not None
    assert rows[already_read["id"]]["read_at"] is not None
    assert client.patch("/api/notifications/read-all").json() == {"updated": 0}


def test_notification_preferences_return_defaults_and_patch_individual_fields(alert_app) -> None:
    client = TestClient(alert_app)

    initial = client.get("/api/notification-preferences")
    assert initial.status_code == 200
    assert initial.json()["persisted"] is False
    assert initial.json()["settings"]["master"] is True
    assert initial.json()["settings"]["marketOpen"] is True
    assert initial.json()["settings"]["aiAnomaly"] is True
    assert initial.json()["settings"]["volumeSpike"] is False
    assert initial.json()["thresholds"] == {"rapidMovePct": 5, "volumeSpikeMultiple": 3}

    updated = client.patch(
        "/api/notification-preferences",
        json={
            "settings": {"marketOpen": False, "targetPrice": False},
            "thresholds": {"rapidMovePct": 10, "volumeSpikeMultiple": 2},
            "companyOverrides": {"aapl": False},
        },
    )

    assert updated.status_code == 200
    assert updated.json()["persisted"] is True
    assert updated.json()["settings"]["marketOpen"] is False
    assert updated.json()["settings"]["targetPrice"] is False
    assert updated.json()["settings"]["rapidMove"] is True
    assert updated.json()["thresholds"] == {"rapidMovePct": 10, "volumeSpikeMultiple": 2}
    assert updated.json()["companyOverrides"] == {"AAPL": False}
    assert client.get("/api/notification-preferences").json() == updated.json()


def test_notification_preferences_reject_unknown_keys_and_empty_patches(alert_app) -> None:
    client = TestClient(alert_app)

    unknown = client.patch(
        "/api/notification-preferences",
        json={"settings": {"unknownSetting": True}},
    )
    empty = client.patch("/api/notification-preferences", json={})
    invalid_threshold = client.patch(
        "/api/notification-preferences",
        json={"thresholds": {"rapidMovePct": 7}},
    )
    unknown_threshold = client.patch(
        "/api/notification-preferences",
        json={"thresholds": {"mysteryThreshold": 3}},
    )

    assert unknown.status_code == 422
    assert "unknownSetting" in unknown.json()["detail"]
    assert empty.status_code == 422
    assert invalid_threshold.status_code == 400
    assert unknown_threshold.status_code == 400


def test_notification_preferences_normalize_legacy_json(alert_app) -> None:
    repository = alert_app.state.notification_preferences_repository
    repository.rows["dev-auth-disabled"] = {
        "user_sub": "dev-auth-disabled",
        "settings": {
            "master": False,
            "rapidMove": False,
            "watchlistNews": False,
            "earningsFiling": False,
            "unknownLegacyKey": True,
        },
        "company_overrides": {"aapl": False},
        "updated_at": "2026-07-14T00:00:00+00:00",
    }

    payload = TestClient(alert_app).get("/api/notification-preferences").json()

    assert set(payload["settings"]) == {
        "master",
        "targetPrice",
        "rapidMove",
        "volumeSpike",
        "marketOpen",
        "marketClose",
        "rsiBand",
        "economicCalendar",
        "earnings",
        "tradingHalt",
        "marketVolatility",
        "extendedHoursMove",
        "socialIssue",
        "aiAnomaly",
    }
    assert payload["settings"]["master"] is False
    assert payload["settings"]["rapidMove"] is False
    assert payload["thresholds"] == {"rapidMovePct": 5, "volumeSpikeMultiple": 3}
    assert payload["companyOverrides"] == {"AAPL": False}


def test_notification_preferences_enforce_total_company_override_limit(alert_app) -> None:
    client = TestClient(alert_app)
    repository = alert_app.state.notification_preferences_repository
    repository.rows["dev-auth-disabled"] = {
        "user_sub": "dev-auth-disabled",
        "settings": {},
        "company_overrides": {f"S{index}": False for index in range(50)},
        "updated_at": "2026-07-14T00:00:00+00:00",
    }

    response = client.patch(
        "/api/notification-preferences",
        json={"companyOverrides": {"AAPL": False}},
    )

    assert response.status_code == 422
    assert "up to 50" in response.json()["detail"]


def test_notification_preferences_are_scoped_by_session_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")

    app = create_app()
    config = AuthConfig.from_env()
    store = MemorySessionStore(config)
    app.state.auth_session_store = store
    app.state.notification_preferences_repository = InMemoryNotificationPreferenceRepository()
    client = TestClient(app)

    client.cookies.set(config.session_cookie_name, store.create_session(AuthenticatedUser("user-1", "one@test.local", True)))
    assert client.patch(
        "/api/notification-preferences",
        json={"settings": {"master": False}},
    ).status_code == 200

    client.cookies.set(config.session_cookie_name, store.create_session(AuthenticatedUser("user-2", "two@test.local", True)))
    second_user = client.get("/api/notification-preferences").json()

    assert second_user["persisted"] is False
    assert second_user["settings"]["master"] is True


def test_delete_all_alerts_only_removes_current_users_alerts(alert_app) -> None:
    repo = alert_app.state.alert_repository
    client = TestClient(alert_app)
    first = client.post(
        "/api/alerts",
        json={"symbol": "NVDA", "type": "price_cross", "targetPrice": "110"},
    ).json()["alert"]
    second = client.post(
        "/api/alerts",
        json={"symbol": "AAPL", "type": "price_cross", "targetPrice": "90"},
    ).json()["alert"]
    other_user_alert = repo.create_alert(
        AlertCreate(
            user_sub="other-user",
            symbol="MSFT",
            type="price_cross",
            direction="above",
            target_price=Decimal("120"),
        )
    )

    response = client.delete("/api/alerts")

    assert response.status_code == 200
    assert response.json() == {"deleted": 2, "projectionStatus": "synced"}
    assert client.get("/api/alerts").json()["alerts"] == []
    assert repo.list_alerts("other-user") == [other_user_alert]
    assert set(alert_app.state.alert_projection.deleted) == {
        (first["id"], "NVDA"),
        (second["id"], "AAPL"),
    }


def test_notification_websocket_requires_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-client-secret")
    app = create_app()
    app.state.auth_session_store = MemorySessionStore(AuthConfig.from_env())

    with TestClient(app) as client:
        with client.websocket_connect("/ws/notifications") as websocket:
            message = websocket.receive_json()

    assert message == {"type": "error", "detail": "authentication required"}
