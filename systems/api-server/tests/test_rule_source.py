"""D23 규칙 출처(source) — 알림·조건주문 생성·조회·기본값·검증."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

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
    from app.alerts.source import normalize_stored_source, source_from_legacy
    from app.main import create_app
    from app.trade_conditions.repository import InMemoryTradeConditionRepository
except Exception as exc:  # pragma: no cover
    pytest.skip(f"rule source tests unavailable: {exc}", allow_module_level=True)


class FakeProjection:
    def upsert_alert(self, alert):
        pass

    def delete_alert(self, alert_id, *, symbol=None):
        pass


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
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
            "side": "buy",
            "direction": "atOrBelow",
            "triggerPrice": 90,
            "limitPrice": 89.5,
            "quantity": 5,
            "executionEnabled": True,
            "alertsEnabled": True,
            "validity": "DAY",
            "missingFields": [],
            "expiresAt": expires_at,
        }],
    }
    return TestClient(app)


def _alert(client, **extra):
    return client.post("/api/alerts", json={"symbol": "nvda", "type": "price_cross", "targetPrice": "110", **extra})


def _condition(client, **extra):
    return client.post("/api/trade-conditions", json={
        "symbol": "nvda", "side": "buy", "direction": "atOrBelow",
        "triggerPrice": "90", "limitPrice": "89.5", "quantity": 3, **extra,
    })


# --- alerts -------------------------------------------------------------------


def test_alert_source_defaults_to_user(client) -> None:
    created = _alert(client)
    assert created.status_code == 201
    assert created.json()["alert"]["source"] == "user"
    assert client.get("/api/alerts").json()["alerts"][0]["source"] == "user"


@pytest.mark.parametrize("source", ["agent", "chart", "user", "default"])
def test_alert_source_round_trips_to_list(client, source) -> None:
    created = _alert(client, source=source)
    assert created.status_code == 201
    assert created.json()["alert"]["source"] == source
    assert client.get("/api/alerts").json()["alerts"][0]["source"] == source


@pytest.mark.parametrize("bad", ["made_up", "manual", "", 3, "CHART "])
def test_alert_unknown_source_is_422_invalid_source(client, bad) -> None:
    if bad == "CHART ":
        # 대소문자·공백은 정규화해서 받아 준다.
        assert _alert(client, source=bad).json()["alert"]["source"] == "chart"
        return
    response = _alert(client, source=bad)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_source"


def test_alert_source_falls_back_to_legacy_fields(client) -> None:
    assert _alert(client, proposalSource="daily_trade").json()["alert"]["source"] == "agent"
    assert _alert(client, createdVia="chart").json()["alert"]["source"] == "chart"
    assert _alert(client, createdVia="ai_coach").json()["alert"]["source"] == "agent"
    # 명시값이 옛 필드보다 우선
    assert _alert(client, createdVia="chart", source="user").json()["alert"]["source"] == "user"


def test_alert_command_sets_user_source(client) -> None:
    response = client.post(
        "/api/alerts/commands",
        headers={"Idempotency-Key": "src-cmd-1"},
        json={"text": "NVDA RSI 70 이상이면 알림 설정해줘"},
    )
    assert response.json()["status"] == "created"
    assert response.json()["alert"]["source"] == "user"


# --- trade conditions ---------------------------------------------------------


def test_trade_condition_source_defaults_to_user_and_alert_mirrors(client) -> None:
    created = _condition(client)
    assert created.status_code == 201
    condition = created.json()["condition"]
    assert condition["source"] == "user"
    assert client.get("/api/trade-conditions").json()["conditions"][0]["source"] == "user"
    linked = [a for a in client.get("/api/alerts").json()["alerts"] if a["id"] == condition["alert_id"]]
    assert linked[0]["source"] == "user"


def test_trade_condition_chart_source_appears_in_list(client) -> None:
    created = _condition(client, source="chart")
    assert created.status_code == 201
    assert client.get("/api/trade-conditions").json()["conditions"][0]["source"] == "chart"
    assert client.get("/api/alerts").json()["alerts"][0]["source"] == "chart"


def test_trade_condition_unknown_source_is_422(client) -> None:
    response = _condition(client, source="robot")
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "invalid_source", "allowed": ["agent", "chart", "user", "default"]}


def test_trade_condition_command_from_agent_proposal_sets_agent(client) -> None:
    response = client.post("/api/trade-conditions/commands", json={
        "text": "이 가격에 예약매매 걸어줘",
        "analysisId": "analysis-1",
        "proposalId": "proposal-1",
    })
    assert response.json()["status"] == "created"
    assert response.json()["condition"]["source"] == "agent"
    assert client.get("/api/alerts").json()["alerts"][0]["source"] == "agent"


# --- backfill mapping (0027 와 같은 규칙) ----------------------------------------


@pytest.mark.parametrize(
    ("created_via", "proposal_source", "expected"),
    [
        ("manual", None, "user"),
        ("agent_chat", None, "user"),
        ("chart", None, "chart"),
        ("ai_coach", None, "agent"),
        ("manual", "entry_habit", "agent"),
        (None, None, "user"),
    ],
)
def test_legacy_source_mapping(created_via, proposal_source, expected) -> None:
    assert source_from_legacy(created_via=created_via, proposal_source=proposal_source) == expected


def test_stored_legacy_manual_reads_as_user() -> None:
    assert normalize_stored_source("manual") == "user"
    assert normalize_stored_source("agent") == "agent"
    assert normalize_stored_source(None) == "user"
