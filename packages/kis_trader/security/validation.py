"""Forbidden field validation."""

from __future__ import annotations

from typing import Any

from kis_trader.domain.status import OrderContractError

FORBIDDEN_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "account_no",
        "acct_no",
        "cano",
        "account_number",
        "kis_appkey",
        "kis_appsecret",
        "appkey",
        "appsecret",
        "app_key",
        "app_secret",
        "access_token",
        "authorization",
        "auth_token",
        "raw_idempotency_key",
        "idempotency_key",
    }
)


def assert_no_forbidden_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in FORBIDDEN_FIELD_NAMES:
                raise OrderContractError(f"forbidden field at {path}.{key}")
            assert_no_forbidden_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_forbidden_fields(child, f"{path}[{index}]")
