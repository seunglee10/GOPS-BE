import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
    from kis_trader.domain.status import OrderStatus
    from kis_trader.persistence.memory import InMemoryOrderRepository
    from tests.kis_trader.fixtures.orders import sample_order_request

    FASTAPI_TESTCLIENT_AVAILABLE = True
except Exception:
    TestClient = None
    FASTAPI_TESTCLIENT_AVAILABLE = False


HEADERS = {"Idempotency-Key": "idem-1"}


@unittest.skipUnless(FASTAPI_TESTCLIENT_AVAILABLE, "FastAPI TestClient is not available")
class IntegratedOrderRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        os.environ["KIS_ENV"] = "demo"
        os.environ["KAFKA_ACCOUNT_ALIAS"] = "demo-account"
        os.environ["IDEMPOTENCY_HASH_SECRET"] = "test-secret"
        self.repository = InMemoryOrderRepository()
        self.app = create_app()
        self.app.state.order_repository = self.repository
        self.client = TestClient(self.app)

    def test_order_contract_is_namespaced_under_api(self):
        response = self.client.get("/api/order-contract")

        self.assertEqual(response.status_code, 200)
        contract = response.json()
        self.assertEqual(contract["submit"]["path"], "/api/orders")
        self.assertEqual(contract["submit"]["accepted_values"]["market"], ["overseas"])
        self.assertEqual(contract["submit"]["accepted_values"]["order_division"], ["00"])
        self.assertEqual(contract["balance"], "GET /api/orders/balance")

    def test_submit_order_requires_idempotency_key(self):
        response = self.client.post("/api/orders", json=sample_order_request())

        self.assertEqual(response.status_code, 400)

    def test_submit_order_records_received_and_command_outbox(self):
        response = self.client.post("/api/orders", json=sample_order_request(), headers=HEADERS)

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], OrderStatus.RECEIVED.value)
        self.assertEqual(self.repository.get_order(payload["order_id"])["status"], OrderStatus.RECEIVED.value)
        self.assertEqual(self.repository.fetch_pending_outbox()[0]["topic"], "orders.commands.v1")

    def test_duplicate_same_body_replays_same_order_id(self):
        first = self.client.post("/api/orders", json=sample_order_request(), headers=HEADERS)
        second = self.client.post("/api/orders", json=sample_order_request(), headers=HEADERS)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["order_id"], second.json()["order_id"])
        self.assertTrue(second.json()["idempotent_replay"])
        self.assertEqual(second.headers["X-Idempotent-Replay"], "true")

    def test_same_idempotency_key_with_different_body_returns_409(self):
        first = self.client.post("/api/orders", json=sample_order_request(), headers=HEADERS)
        second = self.client.post("/api/orders", json=sample_order_request(qty="2"), headers=HEADERS)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)

    def test_forbidden_field_in_api_payload_is_rejected_without_partial_rows(self):
        payload = sample_order_request()
        payload["nested"] = {"access_token": "secret"}

        response = self.client.post("/api/orders", json=payload, headers=HEADERS)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.repository.orders, {})
        self.assertEqual(self.repository.outbox_events, {})

    def test_submit_order_rejects_non_overseas_market(self):
        payload = sample_order_request(market="domestic", symbol="005930", exchange="KRX", price="70000")

        response = self.client.post("/api/orders", json=payload, headers=HEADERS)

        self.assertEqual(response.status_code, 422)
        self.assertIn("market", response.json()["detail"])
        self.assertEqual(self.repository.orders, {})

    def test_submit_order_rejects_non_limit_order_division(self):
        payload = sample_order_request(order_division="01")

        response = self.client.post("/api/orders", json=payload, headers=HEADERS)

        self.assertEqual(response.status_code, 422)
        self.assertIn("order_division", response.json()["detail"])
        self.assertEqual(self.repository.orders, {})

    def test_submit_order_rejects_fractional_quantity(self):
        payload = sample_order_request(qty="1.5")

        response = self.client.post("/api/orders", json=payload, headers=HEADERS)

        self.assertEqual(response.status_code, 422)
        self.assertIn("whole-share", response.json()["detail"])
        self.assertEqual(self.repository.orders, {})

    def test_order_balance_returns_kis_demo_orderable_cash(self):
        class FakeKisClient:
            def fetch_orderable_cash(self, *, symbol: str, exchange: str, price: str):
                return {
                    "env": "demo",
                    "market": "overseas",
                    "symbol": symbol.upper(),
                    "exchange": exchange.upper(),
                    "currency": "USD",
                    "orderable_cash": "12345.67",
                    "orderable_qty": "12",
                }

        with patch("app.routes.orders.DemoKisHttpClient.from_env", return_value=FakeKisClient()):
            response = self.client.get("/api/orders/balance?symbol=nvda&exchange=nasd&price=1.00")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["exchange"], "NASD")
        self.assertEqual(payload["orderable_cash"], "12345.67")

    def test_order_read_and_events_endpoints(self):
        created = self.client.post("/api/orders", json=sample_order_request(), headers=HEADERS).json()

        order = self.client.get(f"/api/orders/{created['order_id']}")
        events = self.client.get(f"/api/orders/{created['order_id']}/events")

        self.assertEqual(order.status_code, 200)
        self.assertEqual(order.json()["order_id"], created["order_id"])
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["events"][0]["status"], OrderStatus.RECEIVED.value)

    def test_websocket_returns_latest_order_snapshot(self):
        created = self.client.post("/api/orders", json=sample_order_request(), headers=HEADERS).json()

        with self.client.websocket_connect(f"/ws/orders/{created['order_id']}") as websocket:
            message = websocket.receive_json()

        self.assertEqual(message["type"], "snapshot")
        self.assertEqual(message["order"]["status"], OrderStatus.RECEIVED.value)


if __name__ == "__main__":
    unittest.main()
