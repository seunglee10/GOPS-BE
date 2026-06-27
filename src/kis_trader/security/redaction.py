"""Recursive redaction for logs, DB JSON, API responses, and Kafka payloads."""

from __future__ import annotations

from typing import Any

SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "account_no",
    "acct_no",
    "cano",
    "account_number",
    "appkey",
    "appsecret",
    "app_key",
    "app_secret",
    "access_token",
    "authorization",
    "auth_token",
    "token",
    "secret",
    "raw_idempotency_key",
    "idempotency_key",
)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_sensitive(child)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(child) for child in value]
    return value
