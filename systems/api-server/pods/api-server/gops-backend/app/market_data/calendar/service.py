from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from alfaka.common.secrets import load_alpaca_credentials


DEFAULT_MARKET_TIMEZONE = "America/New_York"
DEFAULT_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
MARKET_CLOCK_TIMEOUT_SECONDS = 4.0


ClockProvider = Callable[[], dict[str, Any] | None]


def next_market_open_payload(now: datetime | None = None, clock_provider: ClockProvider | None = None) -> dict[str, Any]:
    """Return the next real US regular-market open time as a public API payload."""
    current = normalize_datetime(now)
    clock_payload = _load_clock_payload(clock_provider)
    if clock_payload:
        resolved = _payload_from_clock(clock_payload, current)
        if resolved is not None:
            return resolved

    return _payload_from_configured_calendar(current)


def normalize_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_clock_payload(clock_provider: ClockProvider | None) -> dict[str, Any] | None:
    if clock_provider is not None:
        return clock_provider()
    return fetch_alpaca_clock()


def fetch_alpaca_clock() -> dict[str, Any] | None:
    key_id, secret_key = load_alpaca_credentials()
    if not key_id or not secret_key:
        return None
    endpoint = f"{os.getenv('ALPACA_TRADING_BASE_URL', DEFAULT_TRADING_BASE_URL).rstrip('/')}/v2/clock"
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=MARKET_CLOCK_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None


def _payload_from_clock(payload: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    next_open = parse_datetime(payload.get("next_open") or payload.get("nextOpen"))
    if next_open is None:
        return None
    if next_open <= now:
        return None
    return _render_next_open(
        next_open,
        source="alpaca-clock",
        is_open=bool(payload.get("is_open") or payload.get("isOpen")),
    )


def _payload_from_configured_calendar(now: datetime) -> dict[str, Any]:
    market_zone = ZoneInfo(os.getenv("MARKET_TIMEZONE") or DEFAULT_MARKET_TIMEZONE)
    open_time = parse_market_time(os.getenv("MARKET_OPEN_TIME"), time(9, 30))
    close_time = parse_market_time(os.getenv("MARKET_CLOSE_TIME"), time(16, 0))
    early_closes = parse_early_closes(os.getenv("MARKET_EARLY_CLOSES"))
    closed_dates = configured_closed_dates(now.year, now.year + 2)
    local_now = now.astimezone(market_zone)
    candidate = local_now.date()

    for _ in range(740):
        if is_session_date(candidate, closed_dates):
            local_open = datetime.combine(candidate, open_time, market_zone)
            if local_open.astimezone(timezone.utc) > now:
                local_close = datetime.combine(candidate, early_closes.get(candidate.isoformat(), close_time), market_zone)
                payload = _render_next_open(local_open.astimezone(timezone.utc), source="configured-nyse", is_open=False)
                payload["nextCloseAt"] = local_close.astimezone(timezone.utc).isoformat()
                payload["marketTimezone"] = market_zone.key
                return payload
        candidate += timedelta(days=1)

    raise RuntimeError("Unable to resolve next US market open within two years.")


def _render_next_open(next_open: datetime, *, source: str, is_open: bool) -> dict[str, Any]:
    next_open_utc = normalize_datetime(next_open)
    market_zone = ZoneInfo(os.getenv("MARKET_TIMEZONE") or DEFAULT_MARKET_TIMEZONE)
    local_open = next_open_utc.astimezone(market_zone)
    return {
        "nextOpenAt": next_open_utc.isoformat(),
        "marketDate": local_open.date().isoformat(),
        "marketTimezone": market_zone.key,
        "source": source,
        "isOpen": is_open,
    }


def is_session_date(value: date, closed_dates: set[str]) -> bool:
    return value.weekday() < 5 and value.isoformat() not in closed_dates


def configured_closed_dates(start_year: int, end_year: int) -> set[str]:
    configured = {item for item in parse_csv(os.getenv("MARKET_CLOSED_DATES")) if item}
    include_defaults = str(os.getenv("MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS", "true")).strip().lower()
    if include_defaults in {"0", "false", "no", "off"}:
        return configured
    holidays: set[str] = set(configured)
    for year in range(start_year, end_year + 1):
        holidays.update(us_equity_holidays(year))
    return holidays


def us_equity_holidays(year: int) -> set[str]:
    holidays = {
        observed_date(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        good_friday(year),
        last_weekday(year, 5, 0),
        observed_date(date(year, 6, 19)),
        observed_date(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_date(date(year, 12, 25)),
    }
    return {item.isoformat() for item in holidays}


def observed_date(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    return cursor + timedelta(days=7 * (occurrence - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    cursor = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def good_friday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return normalize_datetime(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_market_time(value: str | None, default: time) -> time:
    if not value:
        return default
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return default


def parse_early_closes(value: str | None) -> dict[str, time]:
    closes: dict[str, time] = {}
    for item in parse_csv(value):
        if "=" not in item:
            continue
        session_date, close_value = [part.strip() for part in item.split("=", 1)]
        try:
            date.fromisoformat(session_date)
            closes[session_date] = time.fromisoformat(close_value)
        except ValueError:
            continue
    return closes


def parse_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]
