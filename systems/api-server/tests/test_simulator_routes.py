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
            "state": "ready" if self.mode == "simulation" else "idle",
            "datasetId": "sp500-top20-20260715-kst-v1",
            "runId": "run-1" if self.mode == "simulation" else None,
            "virtualTime": "2026-07-15T00:00:00+09:00",
            "startTime": "2026-07-15T00:00:00+09:00",
            "endTime": "2026-07-16T00:00:00+09:00",
            "requestedSpeed": 1,
            "effectiveSpeed": 0,
            "processedEventCount": 0,
            "totalEventCount": 10,
            "progress": 0,
            "lagMs": 0,
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

    def set_speed(self, speed):
        self.calls.append(("speed", speed))
        return {**self.status(), "requestedSpeed": speed}

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

    def individual_order(self, *, user_id, symbol, side, quantity, order_type, limit_price, idempotency_key):
        self.calls.append(("individual", user_id, symbol, side, quantity, order_type, limit_price, idempotency_key))
        return {"order": {"order_id": "sim-one", "status": "filled", "symbol": symbol, "side": side, "qty": str(quantity), "order_type": order_type, "simulation": True}}


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
        self.assertEqual(self.trace, [("gateway", "simulation"), ("gateway", "live")])

    def test_operator_can_change_replay_speed(self):
        self.gateway.mode = "simulation"

        response = self.client.put(
            "/api/simulator/speed",
            json={"speed": 300},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requestedSpeed"], 300)
        self.assertIn(("speed", 300), self.gateway.calls)

        invalid = self.client.put("/api/simulator/speed", json={"speed": 2})
        self.assertEqual(invalid.status_code, 422)

    def test_simulation_holdings_replace_kis_with_semiconductor_dummy_account(self):
        self.gateway.mode = "simulation"

        response = self.client.get("/api/account/holdings?market=overseas&currency=USD")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "gops-simulator")
        self.assertEqual({item["symbol"] for item in payload["positions"]}, {"NVDA", "AMD"})
        self.assertIn("SIMULATED", payload["account"]["alias"])

    def test_standard_order_route_forwards_limit_order_to_replay_ledger(self):
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
        self.assertIn(("individual", "dev-auth-disabled", "XOM", "buy", 3, "limit", 140.0, "manual-one"), self.gateway.calls)

    def test_market_order_does_not_require_a_price_in_simulation_mode(self):
        self.gateway.mode = "simulation"

        response = self.client.post(
            "/api/orders",
            headers={"Idempotency-Key": "market-one"},
            json={
                "market": "overseas",
                "symbol": "NVDA",
                "side": "buy",
                "qty": "2",
                "exchange": "NASD",
                "order_type": "market",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["order_type"], "market")
        self.assertIn(("individual", "dev-auth-disabled", "NVDA", "buy", 2, "market", None, "market-one"), self.gateway.calls)


if __name__ == "__main__":
    unittest.main()
