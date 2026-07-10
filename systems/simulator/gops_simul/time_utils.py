from __future__ import annotations

from datetime import UTC, date, datetime, time


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if "T" not in text:
        parsed_date = date.fromisoformat(text)
        return datetime.combine(parsed_date, time.min, tzinfo=UTC)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_record_time(record: dict[str, object]) -> datetime:
    value = record.get("t")
    if not isinstance(value, str) or not value:
        raise ValueError("record timestamp field 't' is required")
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("record timestamp field 't' is invalid")
    return parsed


def iso_date_from_timestamp(value: str) -> str:
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("timestamp is required")
    return parsed.date().isoformat()


def isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
