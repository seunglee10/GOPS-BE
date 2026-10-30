"""D26 가드레일: 설정 검증과 주문 전 점검 규칙 4종."""

from __future__ import annotations

import os
import sys
import types
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

    from app.alerts.repository import AlertCreate, InMemoryAlertRepository
    from app.main import create_app
    from app.services.pretrade_guardrails import GuardrailInput, evaluate_guardrails, has_protective_stop
    from app.services.risk_settings import GUARDRAIL_DEFAULTS, RiskSettingsError, RiskSettingsStore
    from app.stocks.service import StocksService
    from app.trade_conditions.repository import InMemoryTradeConditionRepository, TradeConditionCreate
    from kis_trader.paper.memory import InMemoryPaperTradingRepository
except Exception as exc:  # pragma: no cover
    pytest.skip(f"pretrade guardrail tests unavailable: {exc}", allow_module_level=True)


USER = "dev-auth-disabled"


def _settings(**overrides):
    return {**GUARDRAIL_DEFAULTS, **overrides}


def _input(**overrides):
    values = {
        "side": "buy",
        "symbol": "NVDA",
        "qty": Decimal("1"),
        "price": Decimal("100"),
        "settings": _settings(),
        "day_change_percent": 0.0,
        "equity": Decimal("100000"),
    }
    values.update(overrides)
    return GuardrailInput(**values)


def _by_code(checks, code):
    return next((check for check in checks if check["code"] == code), None)


# --- 규칙 단위 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "severity"),
    [(2.99, "ok"), (3.0, "warn"), (3.01, "warn"), (-5.0, "ok")],
)
def test_chase_buy_boundary(change, severity) -> None:
    check = _by_code(evaluate_guardrails(_input(day_change_percent=change)), "chase_buy")
    assert check["severity"] == severity
    assert check["value"] == change
    assert check["limit"] == 3.0
    if severity == "warn":
        assert check["messageKo"] == f"오늘 {change:+.1f}% 오른 종목이에요"
        assert check["detailKo"] == "급등일 추격 매수 경고가 켜져 있어요"
    else:
        assert check["messageKo"] == "추격 매수가 아니에요"


def test_chase_buy_off_or_unknown_change_is_omitted() -> None:
    assert _by_code(evaluate_guardrails(_input(settings=_settings(chaseBuyWarnPercent=None), day_change_percent=9.0)), "chase_buy") is None
    assert _by_code(evaluate_guardrails(_input(day_change_percent=None)), "chase_buy") is None
    # 매도에는 붙지 않는다
    assert _by_code(evaluate_guardrails(_input(side="sell", day_change_percent=9.0)), "chase_buy") is None


@pytest.mark.parametrize(
    ("change", "severity"),
    [(-1.99, "ok"), (-2.0, "confirm"), (-2.01, "confirm"), (4.0, "ok")],
)
def test_sell_on_drop_boundary(change, severity) -> None:
    check = _by_code(evaluate_guardrails(_input(side="sell", day_change_percent=change)), "sell_on_drop")
    assert check["severity"] == severity
    assert check["limit"] == -2.0
    if severity == "confirm":
        assert check["messageKo"] == f"오늘 {change:+.1f}% 내린 종목을 파는 주문이에요"


def test_sell_on_drop_null_setting_turns_it_off() -> None:
    checks = evaluate_guardrails(_input(side="sell", settings=_settings(confirmSellOnDropPercent=None), day_change_percent=-9.0))
    assert _by_code(checks, "sell_on_drop") is None


def _avg_down(**overrides):
    return _input(position_qty=Decimal("10"), average_price=Decimal("120"), current_price=Decimal("100"), **overrides)


def test_avg_down_without_stop_blocks() -> None:
    check = _by_code(evaluate_guardrails(_avg_down(has_protective_stop=False)), "avg_down_no_stop")
    assert check["severity"] == "block"
    assert check["messageKo"] == "손절가 없이 물타기하는 주문이에요"
    assert check["value"] == pytest.approx(-16.67)


def test_avg_down_with_stop_passes() -> None:
    check = _by_code(evaluate_guardrails(_avg_down(has_protective_stop=True)), "avg_down_no_stop")
    assert check["severity"] == "ok"


def test_avg_down_skipped_when_setting_off_or_not_at_loss_or_unknown() -> None:
    off = _avg_down(has_protective_stop=False, settings=_settings(blockAverageDownWithoutStop=False))
    assert _by_code(evaluate_guardrails(off), "avg_down_no_stop") is None
    at_cost = _input(position_qty=Decimal("10"), average_price=Decimal("100"), current_price=Decimal("100"), has_protective_stop=False)
    assert _by_code(evaluate_guardrails(at_cost), "avg_down_no_stop") is None
    no_position = _input(has_protective_stop=False, current_price=Decimal("100"))
    assert _by_code(evaluate_guardrails(no_position), "avg_down_no_stop") is None
    unknown = _avg_down(has_protective_stop=None)
    assert _by_code(evaluate_guardrails(unknown), "avg_down_no_stop") is None


@pytest.mark.parametrize(
    ("qty", "severity", "weight"),
    [(Decimal("250"), "ok", 25.0), (Decimal("251"), "block", 25.1)],
)
def test_position_weight_boundary(qty, severity, weight) -> None:
    check = _by_code(evaluate_guardrails(_input(qty=qty)), "position_weight")
    assert check["severity"] == severity
    assert check["value"] == weight
    assert check["limit"] == 25.0
    suffix = "한도 25%를 넘어요" if severity == "block" else "한도 25% 안이에요"
    assert check["messageKo"] == f"NVDA 비중 {weight:.0f}%, {suffix}"


def test_position_weight_includes_existing_holding() -> None:
    # 보유 200주 x 110 = 22,000 + 주문 30주 x 100 = 3,000 → 25,000 / 100,000 = 25% (경계, 통과)
    data = _input(qty=Decimal("30"), position_qty=Decimal("200"), current_price=Decimal("110"))
    assert _by_code(evaluate_guardrails(data), "position_weight")["value"] == 25.0
    # 매도에는 붙지 않는다
    assert _by_code(evaluate_guardrails(_input(side="sell", qty=Decimal("900"))), "position_weight") is None


def test_protective_stop_detection() -> None:
    price = Decimal("100")
    sell_stop = {"symbol": "NVDA", "side": "sell", "status": "watching", "target_price": 90.0}
    buy_dip = {"symbol": "NVDA", "side": "buy", "status": "watching", "target_price": 90.0}
    above = {"symbol": "NVDA", "side": "sell", "status": "watching", "target_price": 110.0}
    alert = {"symbol": "NVDA", "status": "active", "type": "price_cross", "direction": "below", "target_price": 95.0, "created_via": "manual"}
    tc_alert = {**alert, "created_via": "trade_condition"}
    assert has_protective_stop("nvda", price, trade_conditions=[sell_stop], alerts=[]) is True
    assert has_protective_stop("NVDA", price, trade_conditions=[buy_dip, above], alerts=[tc_alert]) is False
    assert has_protective_stop("NVDA", price, trade_conditions=[], alerts=[alert]) is True
    assert has_protective_stop("AAPL", price, trade_conditions=[sell_stop], alerts=[alert]) is False


# --- 설정 저장소 -----------------------------------------------------------------


def test_settings_defaults_include_guardrails() -> None:
    values = RiskSettingsStore().active_values("u")
    assert values["chaseBuyWarnPercent"] == Decimal("3.0")
    assert values["blockAverageDownWithoutStop"] is True
    assert values["confirmSellOnDropPercent"] == Decimal("2.0")
    assert values["maxPositionWeightPercent"] == Decimal("25")
    # 엔진 오버라이드에는 섞이지 않는다
    assert RiskSettingsStore().engine_overrides("u") == {}


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("chaseBuyWarnPercent", 0.4, "risk_setting_out_of_range"),
        ("chaseBuyWarnPercent", 30.1, "risk_setting_out_of_range"),
        ("chaseBuyWarnPercent", "abc", "invalid_risk_setting"),
        ("chaseBuyWarnPercent", True, "invalid_risk_setting"),
        ("confirmSellOnDropPercent", -1, "risk_setting_out_of_range"),
        ("maxPositionWeightPercent", 4.9, "risk_setting_out_of_range"),
        ("maxPositionWeightPercent", 100.1, "risk_setting_out_of_range"),
        ("maxPositionWeightPercent", None, "invalid_risk_setting"),
        ("blockAverageDownWithoutStop", "yes", "invalid_risk_setting"),
        ("blockAverageDownWithoutStop", None, "invalid_risk_setting"),
        ("unknownKey", 1, "unknown_risk_setting"),
    ],
)
def test_settings_validation_rejects(key, value, code) -> None:
    with pytest.raises(RiskSettingsError) as caught:
        RiskSettingsStore().apply_change("u", values={key: value}, today="2026-10-30")
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("chaseBuyWarnPercent", 0.5), ("chaseBuyWarnPercent", 30), ("chaseBuyWarnPercent", None),
        ("confirmSellOnDropPercent", None), ("maxPositionWeightPercent", 5), ("maxPositionWeightPercent", 100),
        ("blockAverageDownWithoutStop", False),
    ],
)
def test_settings_validation_accepts_boundaries(key, value) -> None:
    RiskSettingsStore().apply_change("u", values={key: value}, today="2026-10-30")


def test_guardrail_changes_follow_tighten_now_loosen_tomorrow() -> None:
    store = RiskSettingsStore()
    tighten = store.apply_change("u", values={"chaseBuyWarnPercent": 2, "maxPositionWeightPercent": 20}, today="2026-10-30")
    assert set(tighten.applied_now) == {"chaseBuyWarnPercent", "maxPositionWeightPercent"}
    loosen = store.apply_change("u", values={"blockAverageDownWithoutStop": False, "confirmSellOnDropPercent": None}, today="2026-10-30")
    assert loosen.applied_now == {}
    assert loosen.effective_date == "2026-10-31"
    assert store.active_values("u", today="2026-10-30")["blockAverageDownWithoutStop"] is True
    tomorrow = store.active_values("u", today="2026-10-31")
    assert tomorrow["blockAverageDownWithoutStop"] is False
    assert tomorrow["confirmSellOnDropPercent"] is None
    assert tomorrow["chaseBuyWarnPercent"] == Decimal("2")


# --- 라우트 ---------------------------------------------------------------------


class FakeProjection:
    def upsert_alert(self, alert):
        pass

    def delete_alert(self, alert_id, *, symbol=None):
        pass


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("IDEMPOTENCY_HASH_SECRET", "guardrail-test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    application = create_app()
    application.state.paper_trading_repository = InMemoryPaperTradingRepository()
    application.state.paper_symbol_validator = lambda symbol: {
        "symbol": symbol, "assetClass": "us_equity", "tradable": True, "status": "active",
    }
    application.state.paper_subscription_sync = lambda orders, positions: None
    application.state.recommendation_repository = types.SimpleNamespace(upsert_portfolio_snapshot=lambda *_: None)
    application.state.paper_price_resolver = lambda _symbol: {"price": "100", "source": "test"}
    application.state.risk_settings_store = RiskSettingsStore()
    alerts = InMemoryAlertRepository()
    application.state.alert_repository = alerts
    application.state.alert_projection = FakeProjection()
    application.state.trade_condition_repository = InMemoryTradeConditionRepository(alerts)
    application.state.day_change = {"NVDA": 4.2}
    application.state.stocks_service = StocksService(heatmap_provider=lambda: {
        "items": [
            {"symbol": symbol, "lastPrice": 100.0 * (1 + change / 100), "previousClose": 100.0}
            for symbol, change in application.state.day_change.items()
        ],
    })
    return application


def _order(**overrides):
    body = {"market": "overseas", "symbol": "NVDA", "side": "buy", "qty": "10", "price": "100", "exchange": "NASD", "order_division": "00"}
    body.update(overrides)
    return body


def test_risk_settings_get_and_patch_expose_guardrails(app) -> None:
    client = TestClient(app)
    current = client.get("/api/risk/settings").json()
    assert current["active"]["chaseBuyWarnPercent"] == 3.0
    assert current["active"]["blockAverageDownWithoutStop"] is True
    assert current["active"]["confirmSellOnDropPercent"] == 2.0
    assert current["active"]["maxPositionWeightPercent"] == 25
    assert current["active"]["singleNameMaxWeight"] == "0.20"  # 기존 키는 그대로
    assert "chaseBuyWarnPercent" in current["meta"]["adjustable"]
    assert current["meta"]["guardrails"]["maxPositionWeightPercent"] == {"min": 5.0, "max": 100.0, "nullable": False}

    patched = client.patch("/api/risk/settings", json={"values": {"maxPositionWeightPercent": 20}})
    assert patched.status_code == 200
    assert patched.json()["appliedNow"] == {"maxPositionWeightPercent": 20}
    assert patched.json()["active"]["maxPositionWeightPercent"] == 20

    rejected = client.patch("/api/risk/settings", json={"values": {"chaseBuyWarnPercent": 99}})
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "risk_setting_out_of_range"
    assert rejected.json()["detail"]["field"] == "chaseBuyWarnPercent"


def test_pretrade_returns_checks_with_chase_buy_warn(app) -> None:
    response = TestClient(app).post("/api/paper/risk/pretrade", json=_order())
    assert response.status_code == 200
    body = response.json()
    codes = [check["code"] for check in body["checks"]]
    assert codes == ["paper_buying_power", "position_weight", "chase_buy"]
    for check in body["checks"]:
        assert set(check) == {"code", "severity", "messageKo", "detailKo", "value", "limit"}
    assert _by_code(body["checks"], "chase_buy")["severity"] == "warn"
    assert _by_code(body["checks"], "chase_buy")["value"] == 4.2
    assert _by_code(body["checks"], "paper_buying_power")["severity"] == "ok"
    assert body["risk"]["verdict"] == "allow"  # warn 은 막지 않는다


def test_pretrade_position_weight_block_sets_verdict(app) -> None:
    body = TestClient(app).post("/api/paper/risk/pretrade", json=_order(qty="300")).json()
    weight = _by_code(body["checks"], "position_weight")
    assert weight["severity"] == "block"
    assert weight["value"] == 30.0
    assert body["risk"]["verdict"] == "block"
    assert body["risk"]["triggeredRules"][-1]["ruleId"] == "position_weight"


def test_pretrade_sell_on_drop_confirm(app) -> None:
    client = TestClient(app)
    app.state.day_change = {"NVDA": -2.5}
    _buy_and_fill(app, qty="5", price="100")
    body = client.post("/api/paper/risk/pretrade", json=_order(side="sell", qty="1")).json()
    check = _by_code(body["checks"], "sell_on_drop")
    assert check["severity"] == "confirm"
    assert check["messageKo"] == "오늘 -2.5% 내린 종목을 파는 주문이에요"
    assert body["risk"]["verdict"] == "allow"


def test_pretrade_avg_down_without_stop_blocks_then_stop_unblocks(app) -> None:
    _buy_and_fill(app, qty="5", price="120")  # 평균 120, 현재가 100 → 손실 중
    app.state.day_change = {"NVDA": 0.0}
    client = TestClient(app)
    blocked = client.post("/api/paper/risk/pretrade", json=_order(qty="1")).json()
    assert _by_code(blocked["checks"], "avg_down_no_stop")["severity"] == "block"
    assert blocked["risk"]["verdict"] == "block"

    app.state.trade_condition_repository.create_condition(TradeConditionCreate(
        user_sub=USER, source="user", symbol="NVDA", side="sell", direction="atOrBelow",
        trigger_price=Decimal("90"), limit_price=Decimal("89"), quantity=5,
    ))
    allowed = client.post("/api/paper/risk/pretrade", json=_order(qty="1")).json()
    assert _by_code(allowed["checks"], "avg_down_no_stop")["severity"] == "ok"
    assert allowed["risk"]["verdict"] == "allow"


def test_pretrade_avg_down_respects_setting_off(app) -> None:
    _buy_and_fill(app, qty="5", price="120")
    store = app.state.risk_settings_store
    store.apply_change(USER, values={"blockAverageDownWithoutStop": False}, today="2026-10-30")
    store.resolve(USER, today="2026-10-31")  # 푸는 변경은 다음 날 적용
    body = TestClient(app).post("/api/paper/risk/pretrade", json=_order(qty="1")).json()
    assert _by_code(body["checks"], "avg_down_no_stop") is None


def test_pretrade_without_market_data_omits_change_rules(app) -> None:
    app.state.day_change = {}
    body = TestClient(app).post("/api/paper/risk/pretrade", json=_order()).json()
    assert _by_code(body["checks"], "chase_buy") is None
    assert _by_code(body["checks"], "position_weight") is not None


def _buy_and_fill(app, *, qty: str, price: str) -> None:
    repository = app.state.paper_trading_repository
    client = TestClient(app)
    response = client.post(
        "/api/paper/orders",
        json=_order(qty=qty, price=price),
        headers={"Idempotency-Key": f"fill-{qty}-{price}"},
    )
    assert response.status_code == 202, response.text
    repository.match_quote(
        symbol="NVDA",
        bid_price=Decimal(price),
        ask_price=Decimal(price),
        quote_timestamp="2026-10-30T14:00:00Z",
        quote_event_id=f"quote-{qty}-{price}",
        bid_size=Decimal("100000"),
        ask_size=Decimal("100000"),
    )
    snapshot = repository.account_snapshot(USER)
    assert any(row["symbol"] == "NVDA" and row["qty"] > 0 for row in snapshot["positions"]), snapshot


# --- 주문 접수 차단 ---------------------------------------------------------------


def _place(app, key, **overrides):
    return TestClient(app).post("/api/paper/orders", json=_order(**overrides), headers={"Idempotency-Key": key})


def _order_count(app):
    return len(app.state.paper_trading_repository.list_orders(USER, limit=500))


def test_order_avg_down_without_stop_is_409_and_creates_no_order(app) -> None:
    _buy_and_fill(app, qty="5", price="120")
    before = _order_count(app)
    response = _place(app, "avg-down-1", qty="1")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "guardrail_blocked"
    assert [check["code"] for check in detail["checks"]] == ["avg_down_no_stop"]
    assert all(check["severity"] == "block" for check in detail["checks"])
    assert _order_count(app) == before


def test_order_retry_after_settings_change_is_not_stuck_on_409(app) -> None:
    _buy_and_fill(app, qty="5", price="120")
    assert _place(app, "avg-down-retry", qty="1").status_code == 409
    store = app.state.risk_settings_store
    store.apply_change(USER, values={"blockAverageDownWithoutStop": False}, today="2026-10-30")
    store.resolve(USER, today="2026-10-31")
    assert _place(app, "avg-down-retry", qty="1").status_code == 202


def test_order_over_weight_limit_is_409(app) -> None:
    before = _order_count(app)
    response = _place(app, "weight-1", qty="300")
    assert response.status_code == 409
    assert response.json()["detail"]["checks"][0]["code"] == "position_weight"
    assert response.json()["detail"]["checks"][0]["value"] == 30.0
    assert _order_count(app) == before


def test_order_warn_only_is_accepted(app) -> None:
    app.state.day_change = {"NVDA": 8.0}  # chase_buy warn
    response = _place(app, "warn-1", qty="10")
    assert response.status_code == 202


def test_order_replay_of_accepted_key_skips_guardrails(app) -> None:
    assert _place(app, "replay-1", qty="200").status_code == 202  # 20%
    app.state.risk_settings_store.apply_change(USER, values={"maxPositionWeightPercent": 10}, today="2026-10-30")
    replay = _place(app, "replay-1", qty="200")
    assert replay.status_code == 202
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert _place(app, "replay-2", qty="200").status_code == 409


def test_order_capacity_error_still_422_before_guardrails(app) -> None:
    assert _place(app, "cash-1", qty="2000").status_code == 422
