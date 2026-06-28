from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from kis_trader.domain.status import OrderStatus
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.api.app import create_app

from tests.kis_trader.fixtures.orders import sample_order_request


HEADERS = {"Idempotency-Key": "idem-1"}


def make_client():
    repo = InMemoryOrderRepository()
    return TestClient(create_app(repo)), repo


def test_health_endpoint():
    client, _repo = make_client()

    assert client.get("/health").json() == {"status": "ok"}


def test_submit_order_requires_idempotency_key():
    client, _repo = make_client()

    response = client.post("/orders", json=sample_order_request())

    assert response.status_code == 400


def test_submit_order_records_received_and_command_outbox():
    client, repo = make_client()

    response = client.post("/orders", json=sample_order_request(), headers=HEADERS)

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == OrderStatus.RECEIVED.value
    assert repo.get_order(payload["order_id"])["status"] == OrderStatus.RECEIVED.value
    assert repo.fetch_pending_outbox()[0]["topic"] == "orders.commands.v1"


def test_duplicate_same_body_replays_same_order_id():
    client, _repo = make_client()

    first = client.post("/orders", json=sample_order_request(), headers=HEADERS)
    second = client.post("/orders", json=sample_order_request(), headers=HEADERS)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["order_id"] == second.json()["order_id"]
    assert second.json()["idempotent_replay"] is True


def test_same_idempotency_key_with_different_body_returns_409():
    client, _repo = make_client()
    changed = sample_order_request(qty="2")

    first = client.post("/orders", json=sample_order_request(), headers=HEADERS)
    second = client.post("/orders", json=changed, headers=HEADERS)

    assert first.status_code == 202
    assert second.status_code == 409


def test_concurrent_same_idempotency_key_converges_to_one_order():
    client, _repo = make_client()

    def submit():
        return client.post("/orders", json=sample_order_request(), headers=HEADERS).json()

    with ThreadPoolExecutor(max_workers=20) as executor:
        responses = list(executor.map(lambda _: submit(), range(100)))

    assert {response["order_id"] for response in responses} == {responses[0]["order_id"]}


def test_forbidden_field_in_api_payload_is_rejected_without_partial_rows():
    client, repo = make_client()
    payload = sample_order_request()
    payload["nested"] = {"access_token": "secret"}

    response = client.post("/orders", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert repo.orders == {}
    assert repo.outbox_events == {}


def test_order_read_and_events_endpoints():
    client, _repo = make_client()
    created = client.post("/orders", json=sample_order_request(), headers=HEADERS).json()

    order = client.get(f"/orders/{created['order_id']}")
    events = client.get(f"/orders/{created['order_id']}/events")

    assert order.status_code == 200
    assert order.json()["order_id"] == created["order_id"]
    assert events.status_code == 200
    assert events.json()["events"][0]["status"] == OrderStatus.RECEIVED.value


def test_websocket_returns_latest_order_status():
    client, _repo = make_client()
    created = client.post("/orders", json=sample_order_request(), headers=HEADERS).json()

    with client.websocket_connect(f"/ws/orders/{created['order_id']}") as websocket:
        message = websocket.receive_json()

    assert message["type"] == "order.status"
    assert message["order"]["status"] == OrderStatus.RECEIVED.value
