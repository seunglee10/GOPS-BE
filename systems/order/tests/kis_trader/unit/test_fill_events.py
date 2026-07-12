from kis_trader.domain.status import OrderStatus
from kis_trader.domain.topics import ORDERS_FILLS_TOPIC

from systems.order.tests.kis_trader.fixtures.orders import repository_with_published_order


def fill_outbox_events(repo):
    return [event for event in repo.outbox_events.values() if event["topic"] == ORDERS_FILLS_TOPIC]


def walk_to_submitted(repo, order_id):
    repo.update_order_status(order_id, OrderStatus.SUBMITTING)
    repo.update_order_status(order_id, OrderStatus.SUBMITTED)


def test_filled_status_emits_fill_outbox_event():
    repo, envelope, command = repository_with_published_order()
    walk_to_submitted(repo, command.order_id)

    repo.update_order_status(command.order_id, OrderStatus.FILLED)

    events = fill_outbox_events(repo)
    assert len(events) == 1
    fill = events[0]["payload"]
    assert fill["event_type"] == "order.filled"
    assert fill["payload"]["symbol"] == "AAPL"
    assert fill["payload"]["side"] == "buy"
    assert fill["payload"]["qty"] == "1"
    assert fill["payload"]["price"] == "145.00"
    assert fill["payload"]["status"] == OrderStatus.FILLED.value
    assert events[0]["message_key"] == "demo-account:AAPL"


def test_partial_fill_then_fill_emits_two_events():
    repo, envelope, command = repository_with_published_order()
    walk_to_submitted(repo, command.order_id)

    repo.update_order_status(command.order_id, OrderStatus.PARTIALLY_FILLED)
    repo.update_order_status(command.order_id, OrderStatus.FILLED)

    statuses = [event["payload"]["payload"]["status"] for event in fill_outbox_events(repo)]
    assert statuses == [OrderStatus.PARTIALLY_FILLED.value, OrderStatus.FILLED.value]


def test_reconciled_fill_also_emits_fill_event():
    # KIS 데모의 실제 체결 경로(리컨실러)도 fills 이벤트를 내보내야 한다
    repo, envelope, command = repository_with_published_order()
    walk_to_submitted(repo, command.order_id)

    repo.update_reconciled_order(command.order_id, OrderStatus.FILLED, None, execution_id="exec-1", payload={"px": "145.00"})

    events = fill_outbox_events(repo)
    assert len(events) == 1
    assert events[0]["payload"]["payload"]["status"] == OrderStatus.FILLED.value


def test_non_fill_statuses_do_not_emit_fill_events():
    repo, envelope, command = repository_with_published_order()
    walk_to_submitted(repo, command.order_id)

    repo.update_order_status(command.order_id, OrderStatus.CANCELED, "user canceled")

    assert fill_outbox_events(repo) == []
