from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from market_data.alpaca.feed_profiles import market_session_for_datetime
from market_data.common.redis_keys import RedisKeyBuilder


MARKET_TIMEZONE = ZoneInfo("America/New_York")


def active_feed_profile_for(now: datetime | None = None) -> str | None:
    current = now or datetime.now(timezone.utc)
    session = market_session_for_datetime(current, timezone=MARKET_TIMEZONE)
    if session in {"pre", "regular", "after"}:
        return "sip"
    if session == "overnight":
        return "boats"
    return None


def market_session_for_profile(profile: str | None) -> str:
    if not profile:
        return "closed"
    return "extended" if profile == "sip" else "overnight"


def reconcile_active_feed(redis_client, now: datetime | None = None, keys: RedisKeyBuilder | None = None) -> dict[str, str]:
    keys = keys or RedisKeyBuilder()
    current = now or datetime.now(timezone.utc)
    profile = active_feed_profile_for(current)
    profile_value = profile or "none"
    current_profile = read_string(redis_client.get(keys.feed_active_profile()))
    if current_profile != profile_value:
        epoch = int(redis_client.incr(keys.feed_active_epoch()))
    else:
        epoch = int(read_string(redis_client.get(keys.feed_active_epoch())) or 0)

    payload = {
        "activeFeedProfile": profile_value,
        "marketSession": market_session_for_datetime(current, timezone=MARKET_TIMEZONE),
        "epoch": str(epoch),
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "policy": "sip=04:00-20:00 ET, boats=20:00-04:00 ET on active 24/5 equity sessions, none=closed",
    }
    redis_client.set(keys.feed_active_profile(), profile_value)
    redis_client.set(keys.feed_active_epoch(), str(epoch))
    redis_client.set(keys.feed_active(), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if profile:
        redis_client.set(keys.feed_lease(profile), payload["updatedAt"])
        redis_client.expire(keys.feed_lease(profile), 120)
    redis_client.set(keys.feed_switch_state(), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return payload


def read_string(value) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
