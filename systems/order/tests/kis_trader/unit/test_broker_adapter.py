from kis_trader.broker_adapter.adapter import KisBrokerAdapter
from kis_trader.domain.envelope import validate_order_envelope
from kis_trader.domain.status import OrderStatus
from kis_trader.kis.fake import FakeKisClient
from kis_trader.operations.guardrails import TradingGuardrails

from systems.order.tests.kis_trader.fixtures.orders import repository_with_published_order, sample_envelope


def make_adapter(outcomes=None, guardrails=None):
    repo, envelope, _command = repository_with_published_order()
    kis = FakeKisClient(outcomes)
    adapter = KisBrokerAdapter(repo, kis, guardrails=guardrails)
    return adapter, repo, kis, envelope


def test_invalid_schema_goes_to_dlq_without_kis_post():
    adapter, repo, kis, envelope = make_adapter()
    del envelope["payload"]["symbol"]

    result = adapter.process_message(envelope)

    assert result.sent_to_dlq is True
    assert result.status == "DLQ"
    assert kis.submit_calls == 0
    assert len(repo.dlq_events) == 1


def test_success_records_submitted_and_result_outbox():
    adapter, repo, kis, envelope = make_adapter(["success"])

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.SUBMITTED.value
    assert kis.submit_calls == 1
    assert repo.get_order("ord-1")["status"] == OrderStatus.SUBMITTED.value
    [result_event] = [event for event in repo.outbox_events.values() if event["topic"] == "broker.submit-results.v1"]
    assert result_event["payload"]["schema_version"] == 1
    assert result_event["payload"]["event_type"] == "order.submit.resulted"
    assert result_event["payload"]["request_id"] == "req-1"
    assert result_event["payload"]["order_id"] == "ord-1"
    assert result_event["payload"]["client_order_id"] == "coid-1"
    assert result_event["payload"]["account_alias"] == "demo-account"
    assert result_event["payload"]["payload"]["status"] == OrderStatus.SUBMITTED.value


def test_duplicate_command_does_not_repost_to_kis():
    adapter, repo, kis, envelope = make_adapter(["success"])

    first = adapter.process_message(envelope)
    second = adapter.process_message(envelope)

    assert first.status == OrderStatus.SUBMITTED.value
    assert second.skipped_external_submit is True
    assert kis.submit_calls == 1
    assert repo.get_order("ord-1")["status"] == OrderStatus.SUBMITTED.value


def test_existing_intent_before_submitting_continues_without_losing_order():
    adapter, repo, kis, envelope = make_adapter(["success"])
    command = validate_order_envelope(envelope)
    repo.claim_submission_intent(command)

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.SUBMITTED.value
    assert kis.submit_calls == 1
    assert repo.get_order("ord-1")["status"] == OrderStatus.SUBMITTED.value


def test_existing_intent_after_submitting_marks_unknown_without_repost():
    adapter, repo, kis, envelope = make_adapter(["success"])
    command = validate_order_envelope(envelope)
    repo.claim_submission_intent(command)
    repo.update_order_status("ord-1", OrderStatus.SUBMITTING)

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.SUBMIT_FAILED_UNKNOWN.value
    assert result.skipped_external_submit is True
    assert kis.submit_calls == 0
    assert repo.get_order("ord-1")["status"] == OrderStatus.SUBMIT_FAILED_UNKNOWN.value


def test_timeout_records_unknown_and_reprocess_does_not_repost():
    adapter, repo, kis, envelope = make_adapter(["timeout"])

    first = adapter.process_message(envelope)
    second = adapter.process_message(envelope)

    assert first.status == OrderStatus.SUBMIT_FAILED_UNKNOWN.value
    assert second.skipped_external_submit is True
    assert kis.submit_calls == 1
    assert repo.get_order("ord-1")["status"] == OrderStatus.SUBMIT_FAILED_UNKNOWN.value


def test_kill_switch_blocks_kis_post_as_risk_rejected():
    guardrails = TradingGuardrails(global_kill_switch=True)
    adapter, repo, kis, envelope = make_adapter(["success"], guardrails)

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.RISK_REJECTED.value
    assert kis.submit_calls == 0
    assert repo.audit_logs[0]["action"] == "policy_rejected"


def test_token_expired_refreshes_once():
    adapter, _repo, kis, envelope = make_adapter(["token_expired", "success"])

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.SUBMITTED.value
    assert kis.refresh_calls == 1
    assert kis.submit_calls == 2


def test_safe_429_retries_once():
    adapter, _repo, kis, envelope = make_adapter(["safe_429", "success"])

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.SUBMITTED.value
    assert kis.submit_calls == 2


def test_unclear_5xx_records_unknown_without_retry():
    adapter, _repo, kis, envelope = make_adapter(["unsafe_5xx"])

    result = adapter.process_message(envelope)

    assert result.status == OrderStatus.SUBMIT_FAILED_UNKNOWN.value
    assert kis.submit_calls == 1


def test_real_env_command_is_rejected_to_dlq_before_kis_post():
    adapter, repo, kis, _envelope = make_adapter(["success"])
    envelope = sample_envelope(env="real")

    result = adapter.process_message(envelope)

    assert result.status == "DLQ"
    assert kis.submit_calls == 0
    assert len(repo.dlq_events) == 1
