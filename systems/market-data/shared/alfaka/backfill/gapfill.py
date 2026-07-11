from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from alfaka.common.trading_calendar import is_us_equity_early_close_date, is_us_equity_session_date
from alfaka.serving.intervals import normalize_chart_interval


@dataclass(frozen=True)
class TradingCalendar:
    provider: str = "configured-nyse"
    timezone_name: str = "America/New_York"
    open_time: time = time(9, 30)
    close_time: time = time(16, 0)
    closed_dates: frozenset[str] = frozenset()
    early_closes: dict[str, time] | None = None
    include_default_holidays: bool = True

    @classmethod
    def from_environment(cls):
        """환경변수로 설정한 주식장 캘린더를 만듭니다."""
        return cls(
            provider=os.getenv("MARKET_CALENDAR_PROVIDER", "configured-nyse"),
            timezone_name=os.getenv("MARKET_TIMEZONE", "America/New_York"),
            open_time=parse_market_clock_time(os.getenv("MARKET_OPEN_TIME"), time(9, 30)),
            close_time=parse_market_clock_time(os.getenv("MARKET_CLOSE_TIME"), time(16, 0)),
            closed_dates=parse_closed_dates(os.getenv("MARKET_CLOSED_DATES")),
            early_closes=parse_early_closes(os.getenv("MARKET_EARLY_CLOSES")),
            include_default_holidays=os.getenv("MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS", "true").lower() not in {"0", "false", "no", "off"},
        )

    @classmethod
    def crypto_24x7(cls):
        """BTCUSD처럼 매일 24시간 거래되는 crypto용 캘린더를 만듭니다."""
        return cls(
            provider="crypto-24x7",
            timezone_name="UTC",
            open_time=time(0, 0),
            close_time=time(0, 0),
            closed_dates=frozenset(),
            early_closes={},
            include_default_holidays=False,
        )

    @property
    def timezone(self):
        """캘린더가 사용하는 시간대를 ZoneInfo 객체로 반환합니다."""
        return ZoneInfo(self.timezone_name)

    @property
    def is_24x7(self) -> bool:
        """이 캘린더가 휴장 없는 24/7 시장인지 확인합니다."""
        return self.provider == "crypto-24x7"

    def session_close_for(self, session_date: date) -> time:
        """특정 날짜의 장 마감 시간을 조기폐장 설정까지 반영해 반환합니다."""
        early_closes = self.early_closes or {}
        configured = early_closes.get(session_date.isoformat())
        if configured is not None:
            return configured
        if self.include_default_holidays and is_us_equity_early_close_date(session_date):
            return time(13, 0)
        return self.close_time

    def is_session_date(self, session_date: date) -> bool:
        """해당 날짜가 gapfill 대상 거래일인지 판단합니다."""
        if self.is_24x7:
            return True
        return is_us_equity_session_date(
            session_date,
            configured_dates=self.closed_dates,
            include_default_holidays=self.include_default_holidays,
        )


@dataclass(frozen=True)
class GapFillRange:
    start: str
    end: str
    missingCount: int


def detect_gapfill_ranges(start, end, interval, actual_timestamps, calendar=None):
    """기대 bucket과 실제 timestamp를 비교해 비어 있는 구간을 찾습니다."""
    interval = normalize_chart_interval(interval)
    calendar = calendar or TradingCalendar.from_environment()
    expected = expected_bucket_starts(start, end, interval, calendar)
    actual = {to_bucket_start(value, interval, calendar) for value in actual_timestamps or []}
    missing = [timestamp for timestamp in expected if timestamp not in actual]
    return coalesce_bucket_ranges(missing, bucket_delta(interval))


def expected_bucket_starts(start, end, interval, calendar):
    """지정한 구간에서 있어야 할 1m 또는 1D bucket 시작 시각을 계산합니다."""
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
    """1분봉 gapfill 기준으로 기대되는 모든 분 단위 bucket을 만듭니다."""
    if calendar.is_24x7:
        cursor = round_down_minute(start_dt)
        values = []
        while cursor < end_dt:
            values.append(cursor)
            cursor += timedelta(minutes=1)
        return values

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
    """일봉 gapfill 기준으로 기대되는 모든 일 단위 bucket을 만듭니다."""
    if calendar.is_24x7:
        session_date = start_dt.astimezone(timezone.utc).date()
        end_date = end_dt.astimezone(timezone.utc).date()
        values = []
        while session_date <= end_date:
            bucket = datetime.combine(session_date, time(0, 0), timezone.utc)
            if start_dt <= bucket < end_dt:
                values.append(bucket)
            session_date += timedelta(days=1)
        return values

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
    """연속으로 비어 있는 bucket들을 하나의 gapfill 요청 구간으로 합칩니다."""
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
    """interval 하나가 시간상 얼마만큼 이동하는지 timedelta로 반환합니다."""
    interval = normalize_chart_interval(interval)
    if interval == "1m":
        return timedelta(minutes=1)
    if interval == "1D":
        return timedelta(days=1)
    raise ValueError(f"Unsupported source interval for GapFill: {interval}")


def to_bucket_start(value, interval, calendar):
    """임의 timestamp를 해당 interval의 bucket 시작 시각으로 내립니다."""
    parsed = parse_time(value)
    interval = normalize_chart_interval(interval)
    if interval == "1m":
        return round_down_minute(parsed)
    if interval == "1D":
        if calendar.is_24x7:
            parsed_utc = parsed.astimezone(timezone.utc)
            return datetime.combine(parsed_utc.date(), time(0, 0), timezone.utc)
        local = parsed.astimezone(calendar.timezone)
        return datetime.combine(local.date(), time(0, 0), calendar.timezone).astimezone(timezone.utc)
    raise ValueError(f"Unsupported source interval for GapFill: {interval}")


def parse_time(value):
    """문자열이나 datetime 값을 UTC datetime으로 파싱합니다."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def round_down_minute(value):
    """초/마이크로초를 버려 UTC 기준 분 시작 시각으로 맞춥니다."""
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def to_iso(value):
    """UTC datetime을 GOPS에서 쓰는 millisecond ISO 문자열로 변환합니다."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_market_clock_time(value, default):
    """HH:MM 형태의 장 시작/마감 시간을 파싱하고 없으면 기본값을 씁니다."""
    if not value:
        return default
    return time.fromisoformat(value.strip())


def parse_closed_dates(value):
    """쉼표로 전달된 휴장일 목록을 검증된 날짜 집합으로 변환합니다."""
    dates = []
    for item in (value or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        date.fromisoformat(cleaned)
        dates.append(cleaned)
    return frozenset(dates)


def parse_early_closes(value):
    """YYYY-MM-DD=HH:MM 형식의 조기폐장 설정을 날짜별 마감 시간으로 변환합니다."""
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
