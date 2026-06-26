from __future__ import annotations

import hashlib
import hmac
from typing import Any

from kis_trader.contracts.order import canonical_json


def hash_idempotency_key(*, raw_key: str, secret: str) -> str:
    normalized = raw_key.strip()
    if not normalized:
        raise ValueError("Idempotency-Key is required.")
    return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_request_body(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
