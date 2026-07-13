from decimal import Decimal

from fastapi.testclient import TestClient

from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.api.app import create_app
from kis_trader.risk import PositionSnapshot, RiskContext, SymbolMetrics

from systems.order.tests.kis_trader.fixtures.orders import sample_order_request

HEADERS = {"Idempotency-Key": "idem-risk-1"}


def healthy_context():
    return RiskContext(
        account_equity=Decimal("10000"),
        positions=(),
        metrics=SymbolMetrics(
            last_price=Decimal("145.00"),
            average_daily_volume=Decimal("1000000"),
        ),
        daily_pnl=Decimal("0"),
    )


def make_client(context=None):
    provider = (lambda request: context) if context is not None else None
    return TestClient(create_app(InMemoryOrderRepository(), risk_context_provider=provider))


def test_orders_flow_unchanged_without_risk_provider():
    client = make_client()

    response = client.post("/orders", json=sample_order_request(), headers=HEADERS)

    assert response.status_code == 202
    assert "risk" not in response.json()


def test_allowed_order_carries_risk_verdict_in_response():
    client = make_client(healthy_context())

    response = client.post("/orders", json=sample_order_request(qty="1"), headers=HEADERS)

    assert response.status_code == 202
    risk = response.json()["risk"]
    assert risk["verdict"] == "allow"


def test_resize_verdict_rejects_order_with_suggested_qty():
    client = make_client(healthy_context())

    # 60 * 145 = 8700 -> 87% of equity, far over the 20% single-name cap (13 shares).
    response = client.post("/orders", json=sample_order_request(qty="60"), headers=HEADERS)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "risk rejected"
    assert detail["risk"]["verdict"] == "resize"
    assert detail["risk"]["adjustedQty"] == "13"
    rule_ids = {rule["ruleId"] for rule in detail["risk"]["triggeredRules"]}
    assert "single_name_limit" in rule_ids


def test_block_verdict_rejects_order():
    context = RiskContext(
        account_equity=Decimal("10000"),
        metrics=SymbolMetrics(last_price=Decimal("145.00")),
        daily_pnl=Decimal("-400"),  # beyond the 3% daily loss limit
    )
    client = make_client(context)

    response = client.post("/orders", json=sample_order_request(qty="1"), headers=HEADERS)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["risk"]["verdict"] == "block"
    rule_ids = {rule["ruleId"] for rule in detail["risk"]["triggeredRules"]}
    assert "daily_loss_cooldown" in rule_ids


def test_sells_are_not_blocked_by_daily_loss_cooldown():
    context = RiskContext(
        account_equity=Decimal("10000"),
        metrics=SymbolMetrics(last_price=Decimal("145.00")),
        daily_pnl=Decimal("-400"),
    )
    client = make_client(context)

    response = client.post("/orders", json=sample_order_request(side="sell", qty="1"), headers=HEADERS)

    assert response.status_code == 202


def test_pretrade_preview_uses_inline_context_without_provider():
    client = make_client()
    payload = sample_order_request(qty="60")
    payload["risk_context"] = {
        "accountEquity": "10000",
        "dailyPnl": "0",
        "positions": [],
        "metrics": {"lastPrice": "145.00", "averageDailyVolume": "1000000"},
    }

    response = client.post("/risk/pretrade", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["risk"]["verdict"] == "resize"


def test_pretrade_preview_reports_skipped_rules_with_empty_context():
    client = make_client()

    response = client.post("/risk/pretrade", json=sample_order_request())

    assert response.status_code == 200
    risk = response.json()["risk"]
    assert risk["verdict"] == "allow"
    skipped = {item["ruleId"] for item in risk["skippedRules"]}
    assert "single_name_limit" in skipped


def test_pretrade_preview_validates_contract():
    client = make_client()

    response = client.post("/risk/pretrade", json={"symbol": "AAPL"})

    assert response.status_code == 422
