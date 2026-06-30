from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from alfaka.common.env import parse_csv
from alfaka.serving.time_utils import parse_utc_time


MARKET_TIMEZONE = ZoneInfo(os.getenv("MARKET_TIMEZONE", "America/New_York"))
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_MARKET_CLOSE = time(20, 0)


@dataclass(frozen=True)
class FeedProfile:
    profile_id: str
    feed: str
    sessions: tuple[str, ...]
    description: str

    @property
    def websocket_feed(self) -> str:
        return "test" if self.feed == "test" else self.feed

    @property
    def websocket_url(self) -> str:
        feed = self.websocket_feed
        return "wss://stream.data.alpaca.markets/v2/test" if feed == "test" else f"wss://stream.data.alpaca.markets/v2/{feed}"


PROFILE_DEFINITIONS: dict[str, FeedProfile] = {
    "sip": FeedProfile(
        profile_id="sip",
        feed="sip",
        sessions=("pre", "regular", "after"),
        description="SIP daytime US equities feed for pre-market, regular, and after-hours sessions.",
    ),
    "iex": FeedProfile(
        profile_id="iex",
        feed="iex",
        sessions=("pre", "regular", "after"),
        description="IEX daytime US equities feed for pre-market, regular, and after-hours sessions.",
    ),
    "boats": FeedProfile(
        profile_id="boats",
        feed="boats",
        sessions=("overnight", "pre", "regular", "after"),
        description="BOATS/overnight-capable US equities feed for 24/5 coverage.",
    ),
    "overnight": FeedProfile(
        profile_id="overnight",
        feed="boats",
        sessions=("overnight", "pre", "regular", "after"),
        description="Alias for the BOATS/overnight-capable 24/5 feed profile.",
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
    local = parsed.astimezone(timezone)
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


def is_24_5_market_session(session: str | None) -> bool:
    return str(session or "").strip().lower() in {"overnight", "pre", "regular", "after"}
