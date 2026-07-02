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
    extended_open_time: time = time(4, 0)
    extended_close_time: time = time(20, 0)
    closed_dates: frozenset[str] = frozenset()
    early_closes: dict[str, time] | None = None

    @classmethod
    def from_environment(cls):
        return cls(
            provider=os.getenv("MARKET_CALENDAR_PROVIDER", "configured-nyse"),
            timezone_name=os.getenv("MARKET_TIMEZONE", "America/New_York"),
            open_time=parse_market_clock_time(os.getenv("MARKET_OPEN_TIME"), time(9, 30)),
            close_time=parse_market_clock_time(os.getenv("MARKET_CLOSE_TIME"), time(16, 0)),
            extended_open_time=parse_market_clock_time(os.getenv("MARKET_EXTENDED_OPEN_TIME"), time(4, 0)),
            extended_close_time=parse_market_clock_time(os.getenv("MARKET_EXTENDED_CLOSE_TIME"), time(20, 0)),
            closed_dates=parse_closed_dates(os.getenv("MARKET_CLOSED_DATES")),
            early_closes=parse_early_closes(os.getenv("MARKET_EARLY_CLOSES")),
        )

    @property
    def timezone(self):
        return ZoneInfo(self.timezone_name)

    def session_close_for(self, session_date: date) -> time:
        early_closes = self.early_closes or {}
        return early_closes.get(session_date.isoformat(), self.close_time)

    def gapfill_open_for(self, session_date: date) -> time:
        return self.extended_open_time

    def gapfill_close_for(self, session_date: date) -> time:
        early_closes = self.early_closes or {}
        return early_closes.get(session_date.isoformat(), self.extended_close_time)

    def is_session_date(self, session_date: date) -> bool:
        return session_date.weekday() < 5 and session_date.isoformat() not in self.closed_dates


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


def expected_bucket_starts(start, end, interval, calendar):
    interval = normalize_chart_interval(interval)
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
            session_open = datetime.combine(session_date, calendar.gapfill_open_for(session_date), zone)
            session_close = datetime.combine(session_date, calendar.gapfill_close_for(session_date), zone)
            if session_close <= session_open:
                session_date += timedelta(days=1)
                continue
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
            bucket = datetime.combine(session_date, time(0, 0), zone).astimezone(timezone.utc)
            if start_dt <= bucket < end_dt:
                values.append(bucket)
        session_date += timedelta(days=1)
    return values


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
        local = parsed.astimezone(calendar.timezone)
        return datetime.combine(local.date(), time(0, 0), calendar.timezone).astimezone(timezone.utc)
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
