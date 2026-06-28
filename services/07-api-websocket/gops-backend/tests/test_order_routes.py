import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PACKAGES = ROOT / "packages"
BACKEND = ROOT / "services" / "07-api-websocket" / "gops-backend"
for path in (str(PACKAGES), str(BACKEND), str(ROOT)):
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
        self.assertEqual(response.json()["submit"]["path"], "/api/orders")

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
