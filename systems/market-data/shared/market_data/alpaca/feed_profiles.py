from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo

from market_data.common.env import parse_csv
from market_data.common.trading_calendar import configured_closed_dates as shared_configured_closed_dates
from market_data.serving.time_utils import parse_utc_time


MARKET_TIMEZONE = ZoneInfo(os.getenv("MARKET_TIMEZONE", "America/New_York"))
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_MARKET_CLOSE = time(20, 0)
DEFAULT_US_EQUITY_CLOSED_DATES = shared_configured_closed_dates(2026, 2026, {})


@dataclass(frozen=True)
class FeedProfile:
    profile_id: str
    feed: str
    sessions: tuple[str, ...]
    description: str
    websocket_path: str | None = None

    @property
    def websocket_feed(self) -> str:
        """테스트 profile만 Alpaca test feed 이름으로 바꿔 반환합니다."""
        return "test" if self.feed == "test" else self.feed

    @property
    def websocket_url(self) -> str:
        """profile 설정에 맞는 Alpaca WebSocket 접속 URL을 만듭니다."""
        base_url = os.getenv("ALPACA_STREAM_BASE_URL", "wss://stream.data.alpaca.markets").rstrip("/")
        if self.websocket_path:
            return f"{base_url}/{self.websocket_path.lstrip('/')}"
        feed = self.websocket_feed
        return f"{base_url}/v2/test" if feed == "test" else f"{base_url}/v2/{feed}"


PROFILE_DEFINITIONS: dict[str, FeedProfile] = {
    "sip": FeedProfile(
        profile_id="sip",
        feed="sip",
        sessions=("pre", "regular", "after"),
        description="SIP daytime US equities feed for pre-market, regular, and after-hours sessions.",
    ),
    "boats": FeedProfile(
        profile_id="boats",
        feed="boats",
        sessions=("overnight",),
        description="BOATS overnight US equities feed for the 20:00-04:00 ET session.",
        websocket_path="v1beta1/boats",
    ),
    "overnight": FeedProfile(
        profile_id="overnight",
        feed="boats",
        sessions=("overnight",),
        description="Alias for Alpaca's overnight-capable feed profile.",
        websocket_path="v1beta1/overnight",
    ),
    "test": FeedProfile(
        profile_id="test",
        feed="test",
        sessions=("pre", "regular", "after"),
        description="Alpaca test stream profile.",
    ),
    "crypto-us": FeedProfile(
        profile_id="crypto-us",
        feed="crypto",
        sessions=("crypto",),
        description="Alpaca 24/7 crypto feed for US crypto market data.",
        websocket_path=f"v1beta3/crypto/{os.getenv('ALPACA_CRYPTO_LOCATION', 'us')}",
    ),
}


def resolve_feed_profile(environ=None) -> FeedProfile:
    """환경변수에서 사용할 Alpaca feed profile 하나를 결정합니다."""
    environ = environ or os.environ
    profile_name = (
        environ.get("ALPACA_FEED_PROFILE") or
        environ.get("ALPACA_FEED") or
        "sip"
    )
    return feed_profile_by_name(profile_name)


def feed_profile_by_name(value: str | None) -> FeedProfile:
    """문자열 profile 이름을 FeedProfile 설정 객체로 변환합니다."""
    key = str(value or "sip").strip().lower()
    profile = PROFILE_DEFINITIONS.get(key)
    if profile:
        return profile
    raise ValueError(f"Unsupported Alpaca feed profile: {value}")


def configured_feed_profiles(environ=None) -> list[FeedProfile]:
    """여러 수집 profile을 동시에 띄울 때 사용할 profile 목록을 읽습니다."""
    environ = environ or os.environ
    values = parse_csv(environ.get("ALPACA_FEED_PROFILES", ""))
    if not values:
        return [resolve_feed_profile(environ)]
    return [feed_profile_by_name(value) for value in values]


def market_session_for_timestamp(timestamp: str | None, timezone=MARKET_TIMEZONE) -> str:
    """UTC timestamp 문자열을 미국 주식장 세션 이름으로 분류합니다."""
    parsed = parse_utc_time(timestamp)
    if not parsed:
        return "unknown"
    return market_session_for_datetime(parsed, timezone=timezone)


def market_session_for_datetime(value: datetime, timezone=MARKET_TIMEZONE, closed_dates=None) -> str:
    """datetime 값을 pre/regular/after/overnight/closed 세션으로 분류합니다."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime_timezone.utc)
    local = value.astimezone(timezone)
    closed_dates = configured_closed_dates(year=local.year) if closed_dates is None else closed_dates
    local_time = local.time()

    if PRE_MARKET_OPEN <= local_time < REGULAR_OPEN:
        session = "pre"
        trading_date = local.date()
    elif REGULAR_OPEN <= local_time < REGULAR_CLOSE:
        session = "regular"
        trading_date = local.date()
    elif REGULAR_CLOSE <= local_time < AFTER_MARKET_CLOSE:
        session = "after"
        trading_date = local.date()
    elif local_time >= AFTER_MARKET_CLOSE:
        session = "overnight"
        trading_date = local.date() + timedelta(days=1)
    else:
        session = "overnight"
        trading_date = local.date()

    if trading_date.isoformat() in closed_dates or trading_date.weekday() >= 5:
        return "closed"
    return session


def market_session_for_now(now: datetime | None = None, timezone=MARKET_TIMEZONE) -> str:
    """현재 시각 기준의 미국 주식장 세션을 계산합니다."""
    return market_session_for_datetime(now or datetime.now(datetime_timezone.utc), timezone=timezone)


def active_extended_session_window(now: datetime | None = None, timezone=MARKET_TIMEZONE):
    """현재 진행 중인 pre/after/overnight 세션의 UTC 범위를 반환합니다."""
    current = now or datetime.now(datetime_timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime_timezone.utc)
    session = market_session_for_datetime(current, timezone=timezone)
    if session not in {"pre", "after", "overnight"}:
        return None

    local = current.astimezone(timezone)
    if session == "pre":
        start = datetime.combine(local.date(), PRE_MARKET_OPEN, tzinfo=timezone)
        end = datetime.combine(local.date(), REGULAR_OPEN, tzinfo=timezone)
    elif session == "after":
        start = datetime.combine(local.date(), REGULAR_CLOSE, tzinfo=timezone)
        end = datetime.combine(local.date(), AFTER_MARKET_CLOSE, tzinfo=timezone)
    elif local.time() >= AFTER_MARKET_CLOSE:
        start = datetime.combine(local.date(), AFTER_MARKET_CLOSE, tzinfo=timezone)
        end = datetime.combine(local.date() + timedelta(days=1), PRE_MARKET_OPEN, tzinfo=timezone)
    else:
        start = datetime.combine(local.date() - timedelta(days=1), AFTER_MARKET_CLOSE, tzinfo=timezone)
        end = datetime.combine(local.date(), PRE_MARKET_OPEN, tzinfo=timezone)
    return (
        session,
        start.astimezone(datetime_timezone.utc),
        end.astimezone(datetime_timezone.utc),
    )


def visible_extended_session_windows(now: datetime | None = None, timezone=MARKET_TIMEZONE):
    """Chart serving에서 현재 live window로 함께 보여줄 extended 세션 범위를 반환합니다."""
    window = active_extended_session_window(now, timezone=timezone)
    if not window:
        return []
    session, start, end = window
    windows = []
    previous = previous_contiguous_extended_session_window(session, start, timezone=timezone)
    if previous:
        windows.append(previous)
    windows.append((session, start, end))
    return windows


def previous_contiguous_extended_session_window(session: str, start: datetime, timezone=MARKET_TIMEZONE):
    """Overnight/pre로 넘어간 직후에도 바로 직전 extended 세션을 숨기지 않게 합니다."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime_timezone.utc)
    local_start = start.astimezone(timezone)
    if session == "overnight":
        previous_start = datetime.combine(local_start.date(), REGULAR_CLOSE, tzinfo=timezone)
        previous_end = datetime.combine(local_start.date(), AFTER_MARKET_CLOSE, tzinfo=timezone)
        expected_session = "after"
    elif session == "pre":
        previous_start = datetime.combine(local_start.date() - timedelta(days=1), AFTER_MARKET_CLOSE, tzinfo=timezone)
        previous_end = datetime.combine(local_start.date(), PRE_MARKET_OPEN, tzinfo=timezone)
        expected_session = "overnight"
    else:
        return None
    probe = previous_end.astimezone(datetime_timezone.utc) - timedelta(minutes=1)
    if market_session_for_datetime(probe, timezone=timezone) != expected_session:
        return None
    return (
        expected_session,
        previous_start.astimezone(datetime_timezone.utc),
        previous_end.astimezone(datetime_timezone.utc),
    )


def configured_closed_dates(environ=None, *, year: int | None = None) -> frozenset[str]:
    """환경변수와 기본 휴장일을 합쳐 주식장 휴장일 집합을 반환합니다."""
    environ = environ or os.environ
    selected_year = int(year or datetime.now(datetime_timezone.utc).astimezone(MARKET_TIMEZONE).year)
    return shared_configured_closed_dates(selected_year, selected_year, environ)


def next_local_date_is_closed(local: datetime, closed_dates: frozenset[str]) -> bool:
    """Overnight 세션이 다음 거래일 휴장에 걸리면 열지 않습니다."""
    return (local.date() + timedelta(days=1)).isoformat() in closed_dates


def feed_profile_active_for_session(feed_profile: FeedProfile, session: str | None) -> bool:
    """현재 세션에서 해당 profile이 payload를 받을 수 있는 상태인지 판단합니다."""
    if "crypto" in feed_profile.sessions:
        return True
    return str(session or "").strip().lower() in feed_profile.sessions


def is_24_5_market_session(session: str | None) -> bool:
    """미국 주식 24/5 거래 세션에 해당하는지 확인합니다."""
    return str(session or "").strip().lower() in {"overnight", "pre", "regular", "after"}
