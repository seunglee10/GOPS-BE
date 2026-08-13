from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_utc_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def canonical_utc_timestamp(value: Any) -> str | None:
    parsed = parse_utc_time(value)
    if not parsed:
        return str(value) if value else None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.000Z")
