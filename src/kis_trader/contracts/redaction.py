from __future__ import annotations

from typing import Any


REDACTED = "[REDACTED]"

SENSITIVE_KEYS = {
    "access_token",
    "account_no",
    "account_number",
    "acnt_prdt_cd",
    "appkey",
    "appsecret",
    "authorization",
    "cano",
    "idempotency-key",
    "kis_account_no",
    "kis_demo_account_no",
    "kis_real_account_no",
    "raw_idempotency_key",
    "token",
}


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_KEYS:
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value
