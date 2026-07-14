import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
ORDER_TEST_ROOT = ROOT / "systems" / "order"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server" / "gops-backend"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(ORDER_TEST_ROOT), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient

from app.main import create_app
from kis_trader.persistence.memory import InMemoryOrderRepository
from systems.order.tests.kis_trader.fixtures.orders import sample_order_request


class FakeSimulatorGateway:
    def __init__(self, trace=None):
        self.mode = "live"
        self.calls = []
        self.trace = trace

    def status(self):
        return {
            "available": True,
            "mode": self.mode,
            "state": "running" if self.mode == "simulation" else "idle",
            "elapsedSeconds": 0,
            "durationSeconds": 300,
            "breakingNewsAtSeconds": 5,
            "breakingNewsReleased": False,
            "symbols": [],
        }

    def set_mode(self, mode):
        self.mode = mode
        self.calls.append(("mode", mode))
        if self.trace is not None:
            self.trace.append(("gateway", mode))
        return self.status()

    def action(self, action):
        self.calls.append(("action", action))
        return self.status()

    def set_phase(self, phase):
        self.calls.append(("phase", phase))
        return {**self.status(), "phase": phase, "phaseIndex": 6}

    def account(self, user_id):
        self.calls.append(("account", user_id))
        return {
            "status": "ok",
            "source": "gops-simulator",
            "account": {"alias": "반도체 집중형 · SIMULATED", "currency": "USD", "cashForeign": 100},
            "positions": {
                "NVDA": {"symbol": "NVDA", "quantity": 100, "sector": "Information Technology"},
                "AMD": {"symbol": "AMD", "quantity": 50, "sector": "Information Technology"},
            },
            "orders": [],
            "limitations": ["simulation only"],
        }

    def basket_order(self, *, user_id, basket, side):
        self.calls.append(("basket", user_id, basket, side))
        return {"orders": [{"order_id": "sim-basket", "status": "filled", "simulation": True}], "account": self.account(user_id)}

    def individual_order(self, *, user_id, symbol, side, quantity):
        self.calls.append(("individual", user_id, symbol, side, quantity))
        return {"order": {"order_id": "sim-one", "status": "filled", "symbol": symbol, "side": side, "qty": str(quantity), "simulation": True}}

    def news(self):
        return {"news": [], "next_page_token": None}


class FakeSimulatorMarketStateManager:
    def __init__(self, trace):
        self.trace = trace

    def capture(self):
        self.trace.append(("market-state", "capture"))

    def restore(self):
        self.trace.append(("market-state", "restore"))


class SimulatorRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        os.environ["KIS_ENV"] = "demo"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "test-secret"
        self.trace = []
        self.gateway = FakeSimulatorGateway(self.trace)
        self.repository = InMemoryOrderRepository()
        self.app = create_app()
        self.app.state.simulator_gateway = self.gateway
        self.app.state.simulator_market_state_manager = FakeSimulatorMarketStateManager(self.trace)
        self.app.state.order_repository = self.repository
        self.client = TestClient(self.app)

    def test_mode_control_is_exposed_to_the_frontend(self):
        initial = self.client.get("/api/simulator/status")
        started = self.client.put("/api/simulator/mode", json={"mode": "simulation"})
        stopped = self.client.put("/api/simulator/mode", json={"mode": "live"})

        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()["mode"], "live")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["mode"], "simulation")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["mode"], "live")
        self.assertEqual(self.gateway.calls, [("mode", "simulation"), ("mode", "live")])
        self.assertEqual(
            self.trace,
            [
                ("market-state", "capture"),
                ("gateway", "simulation"),
                ("gateway", "live"),
                ("market-state", "restore"),
            ],
        )

    def test_operator_can_jump_to_a_demo_phase_from_the_frontend(self):
        self.gateway.mode = "simulation"

        response = self.client.put(
            "/api/simulator/phase",
            json={"phase": "breaking-event"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["phase"], "breaking-event")
        self.assertIn(("phase", "breaking-event"), self.gateway.calls)

    def test_simulation_holdings_replace_kis_with_semiconductor_dummy_account(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "gops-simulator")
        self.assertEqual({item["symbol"] for item in payload["positions"]}, {"NVDA", "AMD"})
        self.assertIn("SIMULATED", payload["account"]["alias"])

    def test_manual_basket_order_is_filled_only_after_the_user_request(self):
        self.gateway.mode = "simulation"

        response = self.client.post(
            "/api/simulator/orders/basket",
            headers={"Idempotency-Key": "manual-sell"},
            json={"basket": "semiconductor", "side": "sell"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["orders"][0]["simulation"])
        self.assertIn(("basket", "dev-auth-disabled", "semiconductor", "sell"), self.gateway.calls)

    def test_standard_order_route_uses_dummy_ledger_in_simulation_mode(self):
        self.gateway.mode = "simulation"

        response = self.client.post(
            "/api/orders",
            headers={"Idempotency-Key": "manual-one"},
            json=sample_order_request(symbol="XOM", side="buy", qty="3", price="140.00"),
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "filled")
        self.assertTrue(response.json()["simulation"])
        self.assertEqual(self.repository.orders, {})
        self.assertIn(("individual", "dev-auth-disabled", "XOM", "buy", 3), self.gateway.calls)


if __name__ == "__main__":
    unittest.main()
