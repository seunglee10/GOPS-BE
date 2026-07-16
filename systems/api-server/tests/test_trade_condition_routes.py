from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
AGENT_SHARED = ROOT / "systems" / "agent-orchestration" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
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

    from app.alerts.repository import InMemoryAlertRepository
    from app.main import create_app
    from app.trade_conditions.repository import (
        InMemoryTradeConditionRepository,
        PostgresTradeConditionRepository,
        TradeConditionCreate,
        _alert_create,
    )
except Exception as exc:  # pragma: no cover
    pytest.skip(f"trade condition route tests unavailable: {exc}", allow_module_level=True)


class FakeProjection:
    def __init__(self) -> None:
        self.upserted = []
        self.deleted = []

    def upsert_alert(self, alert):
        self.upserted.append(alert)

    def delete_alert(self, alert_id, *, symbol=None):
        self.deleted.append((alert_id, symbol))


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakePostgresConnection:
    def __init__(self):
        self.executed = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))
        if "INSERT INTO alerts" in query:
            return FakeResult({
                "id": 41,
                "symbol": "AMD",
                "direction": "below",
                "target_price": Decimal("531"),
                "notifications_enabled": True,
                "triggered_count": 0,
                "status": "active",
            })
        return FakeResult({
            "id": 9,
            "user_sub": "user-1",
            "alert_id": 41,
            "side": "buy",
            "limit_price": Decimal("530.5"),
            "quantity": Decimal("20"),
            "status": "watching",
        })

    def commit(self):
        self.committed = True


@pytest.fixture
def trade_condition_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("TRADE_CONDITION_COMMANDS_ENABLED", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    app = create_app()
    alerts = InMemoryAlertRepository()
    app.state.alert_repository = alerts
    app.state.alert_projection = FakeProjection()
    app.state.alert_price_provider = lambda symbol: {"symbol": symbol, "price": Decimal("100")}
    app.state.trade_condition_repository = InMemoryTradeConditionRepository(alerts)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    app.state.trade_condition_report_provider = lambda analysis_id, user_sub: {
        "analysisId": analysis_id,
        "tradeConditionProposals": [{
            "proposalId": "proposal-1",
            "analysisId": analysis_id,
            "symbol": "NVDA",
            "exchange": "NASD",
            "side": "buy",
            "direction": "atOrBelow",
            "triggerPrice": 90,
            "limitPrice": 89.5,
            "quantity": None,
            "executionEnabled": True,
            "alertsEnabled": True,
            "validity": "DAY",
            "missingFields": ["quantity"],
            "expiresAt": expires_at,
        }],
    }
    return app


def test_trade_condition_alert_shape_matches_current_alert_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    condition = TradeConditionCreate(
        user_sub="user-1",
        source="manual",
        symbol="AMD",
        side="buy",
        direction="atOrBelow",
        trigger_price=Decimal("531"),
        limit_price=Decimal("530.5"),
        quantity=20,
    )

    alert = _alert_create(condition)
    assert alert.condition == {"kind": "price_cross", "operator": "below", "threshold": 531.0}
    assert alert.created_via == "trade_condition"

    connection = FakePostgresConnection()
    repository = PostgresTradeConditionRepository("postgresql://unused")
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    repository.create_condition(condition)

    alert_query, alert_params = connection.executed[0]
    assert "condition, condition_version, created_via" in " ".join(alert_query.split())
    assert getattr(alert_params[5], "obj", None) == alert.condition
    assert alert_params[6:8] == (1, "trade_condition")
    assert connection.committed is True


def test_manual_trade_condition_crud_updates_the_linked_alert(trade_condition_app) -> None:
    client = TestClient(trade_condition_app)
    created = client.post("/api/trade-conditions", json={
        "symbol": "nvda",
        "side": "buy",
        "direction": "atOrBelow",
        "triggerPrice": "90",
        "limitPrice": "89.5",
        "quantity": 3,
        "alertsEnabled": True,
        "executionEnabled": True,
        "validity": "DAY",
    })

    assert created.status_code == 201
    condition = created.json()["condition"]
    assert condition["symbol"] == "NVDA"
    assert condition["status"] == "watching"
    assert client.get("/api/trade-conditions").json()["conditions"][0]["id"] == condition["id"]

    paused = client.patch(f"/api/trade-conditions/{condition['id']}", json={"status": "paused", "alertsEnabled": False})
    assert paused.status_code == 200
    assert paused.json()["condition"]["status"] == "paused"
    assert paused.json()["condition"]["notifications_enabled"] is False

    deleted = client.delete(f"/api/trade-conditions/{condition['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/trade-conditions").json()["conditions"] == []


def test_agent_command_clarifies_quantity_then_registers_once(trade_condition_app) -> None:
    client = TestClient(trade_condition_app)
    clarification = client.post("/api/trade-conditions/commands", json={
        "text": "이 가격에 예약매매랑 알림 걸어줘",
        "analysisId": "analysis-1",
        "proposalId": "proposal-1",
    })
    assert clarification.json()["status"] == "clarify"

    created = client.post("/api/trade-conditions/commands", json={
        "text": "5주로 등록해줘",
        "analysisId": "analysis-1",
        "proposalId": "proposal-1",
    })
    assert created.status_code == 200
    assert created.json()["status"] == "created"
    assert created.json()["condition"]["quantity"] == 5

    replay = client.post("/api/trade-conditions/commands", json={
        "text": "5주로 등록해줘",
        "analysisId": "analysis-1",
        "proposalId": "proposal-1",
    })
    assert replay.json()["status"] == "created"
    assert replay.json()["idempotentReplay"] is True


def test_unrelated_recommendation_does_not_consume_the_active_proposal(trade_condition_app) -> None:
    response = TestClient(trade_condition_app).post("/api/trade-conditions/commands", json={
        "text": "테슬라도 추천해줘",
        "analysisId": "analysis-1",
        "proposalId": "proposal-1",
    })

    assert response.status_code == 200
    assert response.json()["status"] == "not_matched"


def test_manual_direction_must_cross_from_the_current_price(trade_condition_app) -> None:
    response = TestClient(trade_condition_app).post("/api/trade-conditions", json={
        "symbol": "NVDA",
        "side": "buy",
        "direction": "atOrBelow",
        "triggerPrice": "110",
        "limitPrice": "109",
        "quantity": 1,
    })
    assert response.status_code == 422
    assert "below the current price" in response.json()["detail"]


def test_terminal_condition_cannot_be_resumed(trade_condition_app) -> None:
    client = TestClient(trade_condition_app)
    created = client.post("/api/trade-conditions", json={
        "symbol": "NVDA",
        "side": "buy",
        "direction": "atOrBelow",
        "triggerPrice": "90",
        "limitPrice": "89.5",
        "quantity": 1,
    }).json()["condition"]
    repository = trade_condition_app.state.trade_condition_repository
    repository.claim_trigger(created["alert_id"], "event-terminal")
    repository.finish_execution(created["id"], status="completed", order_id="order-1")

    response = client.patch(f"/api/trade-conditions/{created['id']}", json={"status": "watching"})

    assert response.status_code == 409
