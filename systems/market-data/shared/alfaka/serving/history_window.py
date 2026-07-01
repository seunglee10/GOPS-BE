import calendar
import os
from datetime import datetime, time, timedelta, timezone

from alfaka.serving.intervals import normalize_chart_interval


def max_history_years():
    value = os.getenv("MARKET_DATA_MAX_HISTORY_YEARS", "6")
    try:
        years = int(value)
    except (TypeError, ValueError):
        return 6
    return years if years > 0 else None


def history_floor(interval="1D", now=None):
    years = max_history_years()
    if years is None:
        return None
    interval = normalize_chart_interval(interval)
    base = subtract_years(parse_now(now), years)
    if interval in {"1m", "5m", "10m"}:
        return base
    if interval == "1D":
        return ceil_utc_day(base)
    if interval == "1W":
        return ceil_utc_week(base)
    if interval == "1M":
        return ceil_utc_month(base)
    return base


def history_floor_iso(interval="1D", now=None):
    floor = history_floor(interval, now=now)
    return to_iso(floor) if floor else None


def clamp_range_start(start, interval="1D", now=None):
    floor = history_floor(interval, now=now)
    if floor is None:
        return start, False, None
    parsed = parse_time(start)
    if parsed >= floor:
        return to_iso(parsed), False, to_iso(floor)
    return to_iso(floor), True, to_iso(floor)


def range_ends_before_history(end, interval="1D", now=None):
    floor = history_floor(interval, now=now)
    return bool(floor and parse_time(end) <= floor)


def later_iso(left, right):
    if not left:
        return right
    if not right:
        return left
    return to_iso(max(parse_time(left), parse_time(right)))


def parse_now(now=None):
    if now is not None:
        return parse_time(now)
    env_now = os.getenv("MARKET_DATA_HISTORY_NOW")
    if env_now:
        return parse_time(env_now)
    return datetime.now(timezone.utc)


def parse_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def subtract_years(value, years):
    year = value.year - years
    day = min(value.day, calendar.monthrange(year, value.month)[1])
    return value.replace(year=year, day=day)


def ceil_utc_day(value):
    day_start = datetime.combine(value.date(), time(0, 0), timezone.utc)
    if value == day_start:
        return day_start
    return day_start + timedelta(days=1)


def ceil_utc_week(value):
    day = ceil_utc_day(value)
    days_until_monday = (7 - day.weekday()) % 7
    return day + timedelta(days=days_until_monday)


def ceil_utc_month(value):
    day = ceil_utc_day(value)
    month_start = datetime.combine(day.date().replace(day=1), time(0, 0), timezone.utc)
    if day == month_start:
        return month_start
    if day.month == 12:
        return datetime(day.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(day.year, day.month + 1, 1, tzinfo=timezone.utc)
