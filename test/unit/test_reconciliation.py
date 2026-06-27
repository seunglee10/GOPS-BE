from decimal import Decimal

from kis_trader.broker_adapter.adapter import KisBrokerAdapter
from kis_trader.domain.status import OrderStatus
from kis_trader.kis.fake import FakeKisClient
from kis_trader.reconciliation.reconciler import build_reconciliation_report, match_order_row, reconcile_orders, status_from_order_row

from test.fixtures.orders import repository_with_published_order


def make_unknown_repo():
    repo, envelope, _command = repository_with_published_order()
    KisBrokerAdapter(repo, FakeKisClient(["timeout"])).process_message(envelope)
    return repo


def test_submit_failed_unknown_can_reconcile_to_filled():
    repo = make_unknown_repo()
    rows = [{"client_order_id": "coid-1", "broker_order_id": "kis-1", "status": "filled", "filled_qty": "1", "execution_id": "exec-1"}]

    results = reconcile_orders(repo, rows)

    assert results[0].status == OrderStatus.FILLED.value
    assert repo.get_order("ord-1")["status"] == OrderStatus.FILLED.value
    assert "exec-1" in repo.executions
    [event] = [event for event in repo.outbox_events.values() if event["topic"] == "broker.order-events.v1"]
    assert event["payload"]["schema_version"] == 1
    assert event["payload"]["event_type"] == "order.broker.event.reconciled"
    assert event["payload"]["order_id"] == "ord-1"
    assert event["payload"]["payload"]["filled_qty"] == "1"


def test_submit_failed_unknown_cancel_requires_manual_reconciliation():
    repo = make_unknown_repo()
    rows = [{"client_order_id": "coid-1", "status": "canceled", "filled_qty": "0", "execution_id": "cancel-1"}]

    results = reconcile_orders(repo, rows)

    assert results[0].status == OrderStatus.RECONCILIATION_REQUIRED.value
    assert repo.get_order("ord-1")["status"] == OrderStatus.RECONCILIATION_REQUIRED.value


def test_status_from_canceled_after_manual_confirmation_is_canceled():
    status = status_from_order_row({"status": "canceled", "filled_qty": "0"}, expected_qty=Decimal("1"), current_status=OrderStatus.RECONCILIATION_REQUIRED)

    assert status == OrderStatus.CANCELED


def test_symbol_only_match_is_rejected():
    order = {"account_alias": "demo-account", "symbol": "AAPL", "side": "buy", "qty": "1", "price": "145.00", "occurred_at": "2026-06-27T00:00:00+00:00"}
    rows = [{"symbol": "AAPL", "filled_qty": "1"}]

    assert match_order_row(order, rows) is None


def test_fallback_match_requires_full_shape_and_time_window():
    order = {"account_alias": "demo-account", "symbol": "AAPL", "side": "buy", "qty": "1", "price": "145.00", "occurred_at": "2026-06-27T00:00:00+00:00"}
    row = {"account_alias": "demo-account", "symbol": "AAPL", "side": "buy", "qty": "1", "price": "145.00", "occurred_at": "2026-06-27T00:03:00+00:00", "filled_qty": "1"}

    assert match_order_row(order, [row]) is row


def test_report_alerts_for_internal_unknown_missing_in_kis():
    repo = make_unknown_repo()

    report = build_reconciliation_report(repo, [])

    assert report.alerts[0].alert_type == "internal_order_missing_in_kis"


def test_report_alerts_for_external_order_missing_internal():
    repo, _envelope, _command = repository_with_published_order()
    rows = [{"broker_order_id": "external-only", "account_alias": "demo-account", "symbol": "MSFT", "side": "buy", "qty": "1", "price": "300.00", "occurred_at": "2026-06-27T00:00:00+00:00"}]

    report = build_reconciliation_report(repo, rows)

    assert report.alerts[0].alert_type == "external_order_missing_in_internal"
