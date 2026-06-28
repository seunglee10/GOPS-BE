import pytest

from kis_trader.domain.status import OrderContractError
from kis_trader.security.idempotency import hash_idempotency_key, stable_body_hash
from kis_trader.security.redaction import redact_sensitive
from kis_trader.security.validation import assert_no_forbidden_fields


def test_forbidden_fields_are_detected_deep_in_payload():
    payload = {"outer": [{"nested": {"access_token": "secret"}}]}

    with pytest.raises(OrderContractError, match="forbidden field"):
        assert_no_forbidden_fields(payload)


def test_redaction_removes_sensitive_values_recursively():
    payload = {
        "authorization": "Bearer token",
        "safe": {"raw_idempotency_key": "raw", "symbol": "AAPL"},
        "items": [{"account_no": "12345678"}],
    }

    redacted = redact_sensitive(payload)

    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["safe"]["raw_idempotency_key"] == "[REDACTED]"
    assert redacted["items"][0]["account_no"] == "[REDACTED]"
    assert redacted["safe"]["symbol"] == "AAPL"


def test_idempotency_key_hash_does_not_equal_raw_key():
    raw = "idem-key-001"

    assert hash_idempotency_key(raw, "secret") != raw
    assert hash_idempotency_key(raw, "secret") == hash_idempotency_key(raw, "secret")


def test_body_hash_is_stable_for_json_key_order():
    left = {"symbol": "AAPL", "qty": "1", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "qty": "1", "symbol": "AAPL"}

    assert stable_body_hash(left) == stable_body_hash(right)
