import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
ORDER_TEST_ROOT = ROOT / "systems" / "order"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(ORDER_TEST_ROOT), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from kis_trader.persistence.memory import InMemoryOrderRepository
    from systems.order.tests.kis_trader.fixtures.orders import sample_order_request

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


HEADERS = {"Idempotency-Key": "idem-risk-route-1"}


def healthy_holdings_snapshot(**account_overrides):
    account = {
        "totalValueForeign": "10000",
        "cashForeign": "8000",
        **account_overrides,
    }
    return {
        "account": account,
        "positions": [
            {"symbol": "NVDA", "quantity": "5", "marketValueForeign": "700", "sector": "semiconductor"},
        ],
    }


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class RiskRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        os.environ["KIS_ENV"] = "demo"
        os.environ["KAFKA_ACCOUNT_ALIAS"] = "demo-account"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "test-secret"
        os.environ["RISK_PRETRADE_ENABLED"] = "true"
        self.repository = InMemoryOrderRepository()
        self.app = create_app()
        self.app.state.simulator_gateway = SimpleNamespace(status=lambda: {"mode": "live"})
        self.app.state.order_repository = self.repository
        self.app.state.portfolio_market_data_provider = None
        self.app.state.portfolio_fundamentals_adapter = None
        self.client = TestClient(self.app)

    def tearDown(self):
        os.environ.pop("RISK_PRETRADE_ENABLED", None)

    def set_snapshot(self, payload):
        # AUTH_ENABLED=false resolves the current user to AuthenticatedUser.dev().
        self.app.state.portfolio_holdings_snapshots = {"dev-auth-disabled": payload}

    def test_pretrade_preview_reports_skipped_rules_without_data(self):
        response = self.client.post("/api/risk/pretrade", json=sample_order_request())

        self.assertEqual(response.status_code, 200)
        risk = response.json()["risk"]
        self.assertEqual(risk["verdict"], "allow")
        skipped = {item["ruleId"] for item in risk["skippedRules"]}
        self.assertIn("position_sizing_2pct_atr", skipped)

    def test_pretrade_preview_resizes_from_holdings_snapshot(self):
        self.set_snapshot(healthy_holdings_snapshot())

        # 10000 equity, 20% cap = 2000; NVDA already 700 -> headroom 1300 -> 8 shares at 145
        response = self.client.post("/api/risk/pretrade", json=sample_order_request(symbol="NVDA", qty="20"))

        self.assertEqual(response.status_code, 200)
        risk = response.json()["risk"]
        self.assertEqual(risk["verdict"], "resize")
        rule_ids = {rule["ruleId"] for rule in risk["triggeredRules"]}
        self.assertIn("single_name_limit", rule_ids)
        self.assertEqual(risk["adjustedQty"], "8")

    def test_order_submission_blocked_by_daily_loss_cooldown(self):
        self.set_snapshot(healthy_holdings_snapshot(dailyPnl="-400"))

        response = self.client.post("/api/orders", json=sample_order_request(qty="1"), headers=HEADERS)

        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["reason"], "risk rejected")
        rule_ids = {rule["ruleId"] for rule in detail["risk"]["triggeredRules"]}
        self.assertIn("daily_loss_cooldown", rule_ids)

    def test_allowed_order_carries_risk_verdict(self):
        self.set_snapshot(healthy_holdings_snapshot())

        response = self.client.post("/api/orders", json=sample_order_request(qty="1"), headers=HEADERS)

        self.assertEqual(response.status_code, 202)
        risk = response.json().get("risk")
        self.assertIsNotNone(risk)
        self.assertEqual(risk["verdict"], "allow")

    def test_kill_switch_disables_risk_checks_on_orders(self):
        os.environ["RISK_PRETRADE_ENABLED"] = "false"
        self.set_snapshot(healthy_holdings_snapshot(dailyPnl="-400"))

        response = self.client.post("/api/orders", json=sample_order_request(qty="1"), headers=HEADERS)

        self.assertEqual(response.status_code, 202)
        self.assertNotIn("risk", response.json())


if __name__ == "__main__":
    unittest.main()
