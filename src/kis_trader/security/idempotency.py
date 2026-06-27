"""Idempotency hashing utilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any


def stable_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_body_hash(value: Any) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def hash_idempotency_key(raw_key: str, secret: str | None = None) -> str:
    selected_secret = secret if secret is not None else os.getenv("IDEMPOTENCY_HASH_SECRET", "")
    if selected_secret:
        return hmac.new(selected_secret.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
