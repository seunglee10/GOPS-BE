from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone as datetime_timezone
from zoneinfo import ZoneInfo

from alfaka.common.env import parse_csv
from alfaka.serving.time_utils import parse_utc_time


MARKET_TIMEZONE = ZoneInfo(os.getenv("MARKET_TIMEZONE", "America/New_York"))
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_MARKET_CLOSE = time(20, 0)
DEFAULT_US_EQUITY_CLOSED_DATES = frozenset({
    # 2026 NYSE/Nasdaq full-day holidays. Keep MARKET_CLOSED_DATES additive for
    # emergency closures or future schedule overrides.
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
})


@dataclass(frozen=True)
class FeedProfile:
    profile_id: str
    feed: str
    sessions: tuple[str, ...]
    description: str
    websocket_path: str | None = None

    @property
    def websocket_feed(self) -> str:
        return "test" if self.feed == "test" else self.feed

    @property
    def websocket_url(self) -> str:
        if self.websocket_path:
            return f"wss://stream.data.alpaca.markets/{self.websocket_path.lstrip('/')}"
        feed = self.websocket_feed
        return "wss://stream.data.alpaca.markets/v2/test" if feed == "test" else f"wss://stream.data.alpaca.markets/v2/{feed}"


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
}


def resolve_feed_profile(environ=None) -> FeedProfile:
    environ = environ or os.environ
    profile_name = (
        environ.get("ALPACA_FEED_PROFILE") or
        environ.get("ALPACA_FEED") or
        "sip"
    )
    return feed_profile_by_name(profile_name)


def feed_profile_by_name(value: str | None) -> FeedProfile:
    key = str(value or "sip").strip().lower()
    profile = PROFILE_DEFINITIONS.get(key)
    if profile:
        return profile
    raise ValueError(f"Unsupported Alpaca feed profile: {value}")


def configured_feed_profiles(environ=None) -> list[FeedProfile]:
    environ = environ or os.environ
    values = parse_csv(environ.get("ALPACA_FEED_PROFILES", ""))
    if not values:
        return [resolve_feed_profile(environ)]
    return [feed_profile_by_name(value) for value in values]


def market_session_for_timestamp(timestamp: str | None, timezone=MARKET_TIMEZONE) -> str:
    parsed = parse_utc_time(timestamp)
    if not parsed:
        return "unknown"
    return market_session_for_datetime(parsed, timezone=timezone)


def market_session_for_datetime(value: datetime, timezone=MARKET_TIMEZONE, closed_dates=None) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime_timezone.utc)
    local = value.astimezone(timezone)
    closed_dates = configured_closed_dates() if closed_dates is None else closed_dates
    if local.date().isoformat() in closed_dates:
        return "closed"
    weekday = local.weekday()
    local_time = local.time()
    if weekday >= 5:
        return "closed"
    if PRE_MARKET_OPEN <= local_time < REGULAR_OPEN:
        return "pre"
    if REGULAR_OPEN <= local_time < REGULAR_CLOSE:
        return "regular"
    if REGULAR_CLOSE <= local_time < AFTER_MARKET_CLOSE:
        return "after"
    if weekday < 5:
        return "overnight"
    return "closed"


def market_session_for_now(now: datetime | None = None, timezone=MARKET_TIMEZONE) -> str:
    return market_session_for_datetime(now or datetime.now(datetime_timezone.utc), timezone=timezone)


def configured_closed_dates(environ=None) -> frozenset[str]:
    environ = environ or os.environ
    configured = frozenset(parse_csv(environ.get("MARKET_CLOSED_DATES", "")))
    include_defaults = str(environ.get("MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS", "true")).strip().lower()
    if include_defaults in {"0", "false", "no", "off"}:
        return configured
    return DEFAULT_US_EQUITY_CLOSED_DATES | configured


def feed_profile_active_for_session(feed_profile: FeedProfile, session: str | None) -> bool:
    return str(session or "").strip().lower() in feed_profile.sessions


def is_24_5_market_session(session: str | None) -> bool:
    return str(session or "").strip().lower() in {"overnight", "pre", "regular", "after"}
