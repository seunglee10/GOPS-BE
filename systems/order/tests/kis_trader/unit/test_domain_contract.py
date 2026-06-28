import pytest

from kis_trader.domain import (
    CANONICAL_ORDER_TOPICS,
    CANONICAL_STATUSES,
    ORDER_EVENTS_TOPIC,
    ORDERS_COMMANDS_TOPIC,
    ORDERS_DLQ_TOPIC,
    SUBMIT_RESULTS_TOPIC,
    OrderStatus,
    assert_transition_allowed,
    build_order_message_key,
    validate_order_envelope,
)
from kis_trader.domain.status import OrderContractError

from tests.kis_trader.fixtures.orders import sample_envelope


def test_statuses_are_exactly_the_doc_canonical_set():
    assert CANONICAL_STATUSES == (
        "RECEIVED",
        "PUBLISHED",
        "REJECTED",
        "RISK_REJECTED",
        "SUBMITTING",
        "SUBMITTED",
        "SUBMIT_FAILED_UNKNOWN",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "RECONCILIATION_REQUIRED",
        "FAILED",
    )
    assert "VALIDATED" not in CANONICAL_STATUSES
    assert "SUBMIT_REJECTED" not in CANONICAL_STATUSES


def test_topics_are_exactly_the_doc_canonical_set():
    assert CANONICAL_ORDER_TOPICS == (
        ORDERS_COMMANDS_TOPIC,
        SUBMIT_RESULTS_TOPIC,
        ORDER_EVENTS_TOPIC,
        ORDERS_DLQ_TOPIC,
    )


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (OrderStatus.RECEIVED, OrderStatus.PUBLISHED),
        (OrderStatus.SUBMITTING, OrderStatus.SUBMITTED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.FILLED),
        (OrderStatus.RECONCILIATION_REQUIRED, OrderStatus.CANCELED),
    ],
)
def test_allowed_transitions(current, new):
    assert_transition_allowed(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (OrderStatus.PUBLISHED, OrderStatus.REJECTED),
        (OrderStatus.SUBMIT_FAILED_UNKNOWN, OrderStatus.CANCELED),
        (OrderStatus.FILLED, OrderStatus.SUBMITTED),
    ],
)
def test_forbidden_transitions_raise(current, new):
    with pytest.raises(OrderContractError):
        assert_transition_allowed(current, new)


def test_envelope_requires_schema_version_one_and_required_fields():
    envelope = sample_envelope(schema_version=2)
    with pytest.raises(OrderContractError, match="schema_version"):
        validate_order_envelope(envelope)

    envelope = sample_envelope()
    del envelope["client_order_id"]
    with pytest.raises(OrderContractError, match="client_order_id"):
        validate_order_envelope(envelope)


def test_valid_envelope_is_normalized():
    envelope = sample_envelope()
    envelope["payload"]["symbol"] = "aapl"

    command = validate_order_envelope(envelope)

    assert command.symbol == "AAPL"
    assert command.env == "demo"


def test_kafka_message_key_uses_account_alias_and_symbol():
    assert build_order_message_key("demo-account", "aapl") == "demo-account:AAPL"
