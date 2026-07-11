from __future__ import annotations

import os
from datetime import date, timedelta
from functools import lru_cache
from typing import Mapping


# Full-day exceptional US equity market closures that cannot be derived from
# the regular holiday rules.  The analysis horizon is currently bounded to the
# recent decade, but retaining the well-known older closures keeps this helper
# safe for other canonical gap audits.
US_EQUITY_SPECIAL_CLOSED_DATES = frozenset({
    "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",
    "2004-06-11", "2007-01-02", "2012-10-29", "2012-10-30",
    "2018-12-05", "2025-01-09",
})


def us_equity_holidays(year: int) -> set[date]:
    return set(_us_equity_holidays(year))


@lru_cache(maxsize=32)
def _us_equity_holidays(year: int) -> frozenset[date]:
    holidays = {
        observed_date(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        good_friday(year),
        last_weekday(year, 5, 0),
        observed_date(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_date(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(observed_date(date(year, 6, 19)))
    return frozenset(holidays)


def configured_closed_dates(
    start_year: int,
    end_year: int,
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    if environ is None:
        environ = os.environ
    configured = {
        item.strip()
        for item in str(environ.get("MARKET_CLOSED_DATES") or "").split(",")
        if item.strip()
    }
    for item in configured:
        date.fromisoformat(item)
    include_defaults = str(environ.get("MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS", "true")).strip().lower()
    if include_defaults in {"0", "false", "no", "off"}:
        return frozenset(configured)
    values = set(configured) | set(US_EQUITY_SPECIAL_CLOSED_DATES)
    # An observed New Year's Day may land in the preceding calendar year.
    for year in range(start_year - 1, end_year + 2):
        values.update(item.isoformat() for item in us_equity_holidays(year))
    return frozenset(item for item in values if start_year <= date.fromisoformat(item).year <= end_year)


def is_us_equity_session_date(
    value: date,
    *,
    configured_dates: frozenset[str] | set[str] | None = None,
    include_default_holidays: bool = True,
) -> bool:
    if value.weekday() >= 5:
        return False
    key = value.isoformat()
    if key in (configured_dates or ()):
        return False
    if include_default_holidays:
        regular_holidays = (
            _us_equity_holidays(value.year - 1)
            | _us_equity_holidays(value.year)
            | _us_equity_holidays(value.year + 1)
        )
        if key in US_EQUITY_SPECIAL_CLOSED_DATES or value in regular_holidays:
            return False
    return True


def is_us_equity_early_close_date(value: date) -> bool:
    """Return whether the regular NYSE session conventionally closes at 13:00 ET."""
    if not is_us_equity_session_date(value):
        return False
    thanksgiving = nth_weekday(value.year, 11, 3, 4)
    if value == thanksgiving + timedelta(days=1):
        return True
    if value.month == 12 and value.day == 24:
        return True
    independence_day = date(value.year, 7, 4)
    if value.month == 7 and value.day == 3:
        return True
    observed_independence_day = observed_date(independence_day)
    return observed_independence_day.weekday() == 4 and value == observed_independence_day - timedelta(days=1)


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
