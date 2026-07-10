from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from gops_simul.errors import BadRequest


@dataclass(frozen=True)
class PageCursor:
    fingerprint: str
    offset: int


def fingerprint_for(parts: dict[str, Any]) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def encode_cursor(fingerprint: str, offset: int) -> str:
    payload = json.dumps({"f": fingerprint, "o": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str | None, expected_fingerprint: str) -> PageCursor:
    if not value:
        return PageCursor(expected_fingerprint, 0)
    padded = value + "=" * (-len(value) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise BadRequest("invalid page_token") from exc
    if payload.get("f") != expected_fingerprint:
        raise BadRequest("page_token does not match the current query")
    offset = payload.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise BadRequest("invalid page_token offset")
    return PageCursor(expected_fingerprint, offset)
