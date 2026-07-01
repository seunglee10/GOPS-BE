from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from alfaka.serving.intervals import normalize_chart_interval


@dataclass(frozen=True)
class TradingCalendar:
    provider: str = "configured-nyse"
    timezone_name: str = "America/New_York"
    open_time: time = time(9, 30)
    close_time: time = time(16, 0)
    closed_dates: frozenset[str] = frozenset()
    early_closes: dict[str, time] | None = None

    @classmethod
    def from_environment(cls):
        return cls(
            provider=os.getenv("MARKET_CALENDAR_PROVIDER", "configured-nyse"),
            timezone_name=os.getenv("MARKET_TIMEZONE", "America/New_York"),
            open_time=parse_market_clock_time(os.getenv("MARKET_OPEN_TIME"), time(9, 30)),
            close_time=parse_market_clock_time(os.getenv("MARKET_CLOSE_TIME"), time(16, 0)),
            closed_dates=parse_closed_dates(os.getenv("MARKET_CLOSED_DATES")),
            early_closes=parse_early_closes(os.getenv("MARKET_EARLY_CLOSES")),
        )

    @property
    def timezone(self):
        return ZoneInfo(self.timezone_name)

    def session_close_for(self, session_date: date) -> time:
        early_closes = self.early_closes or {}
        return early_closes.get(session_date.isoformat(), self.close_time)

    def is_session_date(self, session_date: date) -> bool:
        if session_date.weekday() >= 5 or session_date.isoformat() in self.closed_dates:
            return False
        if self.provider in {"configured-nyse", "nyse"} and is_default_nyse_holiday(session_date):
            return False
        return True


@dataclass(frozen=True)
class GapFillRange:
    start: str
    end: str
    missingCount: int


def detect_gapfill_ranges(start, end, interval, actual_timestamps, calendar=None):
    interval = normalize_chart_interval(interval)
    calendar = calendar or TradingCalendar.from_environment()
    expected = expected_bucket_starts(start, end, interval, calendar)
    actual = {to_bucket_start(value, interval, calendar) for value in actual_timestamps or []}
    missing = [timestamp for timestamp in expected if timestamp not in actual]
    return coalesce_bucket_ranges(missing, bucket_delta(interval))


def expected_bucket_starts(start, end, interval, calendar=None):
    interval = normalize_chart_interval(interval)
    calendar = calendar or TradingCalendar.from_environment()
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if start_dt >= end_dt:
        return []
    if interval == "1m":
        return expected_minute_buckets(start_dt, end_dt, calendar)
    if interval == "1D":
        return expected_daily_buckets(start_dt, end_dt, calendar)
    raise ValueError(f"GapFill detection supports canonical source intervals only: {interval}")


def expected_minute_buckets(start_dt, end_dt, calendar):
    zone = calendar.timezone
    local_start = start_dt.astimezone(zone)
    local_end = end_dt.astimezone(zone)
    session_date = local_start.date()
    values = []
    while session_date <= local_end.date():
        if calendar.is_session_date(session_date):
            session_open = datetime.combine(session_date, calendar.open_time, zone)
            session_close = datetime.combine(session_date, calendar.session_close_for(session_date), zone)
            cursor = max(round_down_minute(local_start), session_open)
            session_end = min(local_end, session_close)
            while cursor < session_end:
                values.append(cursor.astimezone(timezone.utc))
                cursor += timedelta(minutes=1)
        session_date += timedelta(days=1)
    return values


def expected_daily_buckets(start_dt, end_dt, calendar):
    zone = calendar.timezone
    session_date = start_dt.astimezone(zone).date()
    end_date = end_dt.astimezone(zone).date()
    values = []
    while session_date <= end_date:
        if calendar.is_session_date(session_date):
            bucket = daily_bucket_for_session_date(session_date)
            if start_dt <= bucket < end_dt:
                values.append(bucket)
        session_date += timedelta(days=1)
    return values


def daily_bucket_for_session_date(session_date):
    if isinstance(session_date, datetime):
        session_date = session_date.date()
    return datetime.combine(session_date, time(0, 0), timezone.utc)


def canonical_daily_bucket_start(value):
    parsed = parse_time(value)
    return datetime.combine(parsed.date(), time(0, 0), timezone.utc)


def canonical_daily_timestamp(value):
    return to_iso(canonical_daily_bucket_start(value))


def coalesce_bucket_ranges(missing_buckets, delta):
    ranges = []
    if not missing_buckets:
        return ranges
    sorted_missing = sorted(missing_buckets)
    start = previous = sorted_missing[0]
    count = 1
    for bucket in sorted_missing[1:]:
        if bucket == previous + delta:
            previous = bucket
            count += 1
            continue
        ranges.append(GapFillRange(to_iso(start), to_iso(previous + delta), count))
        start = previous = bucket
        count = 1
    ranges.append(GapFillRange(to_iso(start), to_iso(previous + delta), count))
    return ranges


def bucket_delta(interval):
    interval = normalize_chart_interval(interval)
    if interval == "1m":
        return timedelta(minutes=1)
    if interval == "1D":
        return timedelta(days=1)
    raise ValueError(f"Unsupported source interval for GapFill: {interval}")


def to_bucket_start(value, interval, calendar):
    parsed = parse_time(value)
    interval = normalize_chart_interval(interval)
    if interval == "1m":
        return round_down_minute(parsed)
    if interval == "1D":
        return canonical_daily_bucket_start(parsed)
    raise ValueError(f"Unsupported source interval for GapFill: {interval}")


def parse_time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def round_down_minute(value):
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_market_clock_time(value, default):
    if not value:
        return default
    return time.fromisoformat(value.strip())


def parse_closed_dates(value):
    dates = []
    for item in (value or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        date.fromisoformat(cleaned)
        dates.append(cleaned)
    return frozenset(dates)


def parse_early_closes(value):
    closes = {}
    for item in (value or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if "=" not in cleaned:
            raise ValueError("MARKET_EARLY_CLOSES entries must use YYYY-MM-DD=HH:MM format.")
        session, close_time = [part.strip() for part in cleaned.split("=", 1)]
        date.fromisoformat(session)
        closes[session] = parse_market_clock_time(close_time, time(16, 0))
    return closes


def is_default_nyse_holiday(session_date):
    year = session_date.year
    holidays = {
        observed_fixed_holiday(year, 1, 1),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter_sunday(year) - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed_fixed_holiday(year, 7, 4),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_fixed_holiday(year, 12, 25),
        observed_fixed_holiday(year + 1, 1, 1),
    }
    if year >= 2022:
        holidays.add(observed_fixed_holiday(year, 6, 19))
    return session_date in holidays


def observed_fixed_holiday(year, month, day):
    value = date(year, month, day)
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def nth_weekday(year, month, weekday, occurrence):
    value = date(year, month, 1)
    offset = (weekday - value.weekday()) % 7
    return value + timedelta(days=offset + (occurrence - 1) * 7)


def last_weekday(year, month, weekday):
    if month == 12:
        value = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        value = date(year, month + 1, 1) - timedelta(days=1)
    return value - timedelta(days=(value.weekday() - weekday) % 7)


def easter_sunday(year):
    # Anonymous Gregorian algorithm.
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
    return date(year, month, day)
