from __future__ import annotations

import os
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

sys.modules.setdefault("redis", types.SimpleNamespace(from_url=lambda *args, **kwargs: None))

from app.alerts.repository import InMemoryAlertRepository
from app.trade_conditions.executor import process_trigger_event
from app.trade_conditions.repository import InMemoryTradeConditionRepository, TradeConditionCreate
from kis_trader.paper import InMemoryPaperTradingRepository
from kis_trader.persistence.memory import InMemoryOrderRepository


class FakeApp:
    def __init__(self) -> None:
        self.state = types.SimpleNamespace()


def build_condition_app(*, side="buy", execution_enabled=True):
    app = FakeApp()
    alerts = InMemoryAlertRepository()
    conditions = InMemoryTradeConditionRepository(alerts)
    app.state.alert_repository = alerts
    app.state.trade_condition_repository = conditions
    app.state.paper_trading_repository = InMemoryPaperTradingRepository()
    app.state.order_repository = InMemoryOrderRepository()
    created = conditions.create_condition(TradeConditionCreate(
        user_sub="user-1",
        source="manual",
        symbol="NVDA",
        side=side,
        direction="atOrBelow" if side == "buy" else "atOrAbove",
        trigger_price=Decimal("90") if side == "buy" else Decimal("110"),
        limit_price=Decimal("89.5") if side == "buy" else Decimal("110.5"),
        quantity=5,
        execution_enabled=execution_enabled,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    event = {
        "type": "alert.price_cross",
        "eventId": "event-1",
        "alertId": created["alert_id"],
        "triggeredAt": datetime.now(timezone.utc).isoformat(),
    }
    return app, conditions, created, event


def test_sim_trigger_submits_one_paper_order_and_deduplicates_event(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PAPER_REPOSITORY", "memory")
    app, conditions, created, event = build_condition_app()

    first = process_trigger_event(app, event, mode="sim")
    second = process_trigger_event(app, event, mode="sim")

    assert first["status"] == "completed"
    assert first["order"]["status"] == "pending"
    assert conditions.get_condition("user-1", created["id"])["status"] == "completed"
    assert second["status"] == "duplicate_or_unowned"


def test_sim_sell_is_blocked_when_the_paper_account_has_no_position():
    app, conditions, created, event = build_condition_app(side="sell")

    result = process_trigger_event(app, event, mode="sim")

    assert result["status"] == "blocked"
    assert conditions.get_condition("user-1", created["id"])["status"] == "blocked"


def test_alert_only_condition_does_not_submit_an_order():
    app, conditions, created, event = build_condition_app(execution_enabled=False)

    result = process_trigger_event(app, event, mode="sim")

    assert result["status"] == "triggered"
    assert result["orderSubmitted"] is False
    assert conditions.get_condition("user-1", created["id"])["status"] == "triggered"


def test_demo_mode_uses_existing_order_contract_with_deterministic_idempotency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ORDER_REPOSITORY", "memory")
    monkeypatch.setenv("KIS_ENV", "demo")
    monkeypatch.setenv("TRADE_CONDITION_RISK_REQUIRED", "false")
    app, conditions, created, event = build_condition_app()

    result = process_trigger_event(app, event, mode="demo")

    assert result["status"] == "completed"
    assert result["order"]["status"] == "RECEIVED"
    assert result["condition"]["order_id"] == result["order"]["order_id"]


def test_same_event_recovers_after_order_submission_before_condition_finish(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PAPER_REPOSITORY", "memory")
    app, conditions, created, event = build_condition_app()
    original_finish = conditions.finish_execution
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary condition status write failure")
        return original_finish(*args, **kwargs)

    conditions.finish_execution = fail_once

    with pytest.raises(ConnectionError):
        process_trigger_event(app, event, mode="sim")
    recovered = process_trigger_event(app, event, mode="sim")

    assert recovered["status"] == "completed"
    assert conditions.get_condition("user-1", created["id"])["status"] == "completed"
    paper_orders = app.state.paper_trading_repository.list_orders("user-1")
    assert len(paper_orders) == 1


def test_expired_condition_cannot_claim_a_late_alert_event():
    app, conditions, created, event = build_condition_app()
    conditions.conditions[created["id"]]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    result = process_trigger_event(app, event, mode="sim")

    assert result["status"] == "duplicate_or_unowned"
    assert conditions.get_condition("user-1", created["id"])["status"] == "expired"
    assert app.state.paper_trading_repository.list_orders("user-1") == []
