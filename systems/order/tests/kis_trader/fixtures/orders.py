from __future__ import annotations

from datetime import datetime, timezone

from kis_trader.domain.commands import validate_order_request_payload
from kis_trader.domain.envelope import build_order_command_envelope, validate_order_envelope
from kis_trader.outbox.producer import RecordingProducer
from kis_trader.outbox.publisher import publish_pending_outbox
from kis_trader.persistence.memory import InMemoryOrderRepository
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash


def sample_order_request(**overrides):
    payload = {
        "account_alias": "demo-account",
        "market": "overseas",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "price": "145.00",
        "exchange": "NASD",
        "order_division": "00",
    }
    payload.update(overrides)
    return payload


def sample_envelope(**overrides):
    request = validate_order_request_payload(sample_order_request(), default_account_alias="demo-account")
    envelope = build_order_command_envelope(
        request,
        occurred_at="2026-06-27T00:00:00+00:00",
        request_id="req-1",
        order_id="ord-1",
        client_order_id="coid-1",
        event_id="evt-1",
    )
    envelope.update(overrides)
    return envelope


def sample_command(**overrides):
    return validate_order_envelope(sample_envelope(**overrides))


def repository_with_received_order(payload=None, *, user_sub: str | None = None):
    payload = payload or sample_order_request()
    request = validate_order_request_payload(payload, default_account_alias="demo-account")
    envelope = build_order_command_envelope(
        request,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        request_id="req-1",
        order_id="ord-1",
        client_order_id="coid-1",
        event_id="evt-1",
    )
    command = validate_order_envelope(envelope)
    repo = InMemoryOrderRepository()
    repo.create_received_order(
        idempotency_key_hash=hash_idempotency_key("idem-1", "test-secret"),
        body_hash=stable_body_hash(payload),
        command=command,
        user_sub=user_sub,
    )
    return repo, envelope, command


def repository_with_published_order(payload=None):
    repo, envelope, command = repository_with_received_order(payload)
    publish_pending_outbox(repo, RecordingProducer())
    return repo, envelope, command
