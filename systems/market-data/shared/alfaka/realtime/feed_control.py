from __future__ import annotations

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from alfaka.common.redis_keys import RedisKeyBuilder


MARKET_TIMEZONE = ZoneInfo("America/New_York")
SIP_START = time(4, 0)
BOATS_START = time(20, 0)


def active_feed_profile_for(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(MARKET_TIMEZONE)
    return "sip" if SIP_START <= local.time() < BOATS_START else "boats"


def market_session_for_profile(profile: str) -> str:
    return "extended" if profile == "sip" else "overnight"


def reconcile_active_feed(redis_client, now: datetime | None = None, keys: RedisKeyBuilder | None = None) -> dict[str, str]:
    keys = keys or RedisKeyBuilder()
    profile = active_feed_profile_for(now)
    current_profile = read_string(redis_client.get(keys.feed_active_profile()))
    if current_profile != profile:
        epoch = int(redis_client.incr(keys.feed_active_epoch()))
    else:
        epoch = int(read_string(redis_client.get(keys.feed_active_epoch())) or 0)

    payload = {
        "activeFeedProfile": profile,
        "marketSession": market_session_for_profile(profile),
        "epoch": str(epoch),
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "policy": "sip=04:00-20:00 ET, boats=20:00-04:00 ET, mutual-exclusive",
    }
    redis_client.set(keys.feed_active_profile(), profile)
    redis_client.set(keys.feed_active_epoch(), str(epoch))
    redis_client.set(keys.feed_active(), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    redis_client.set(keys.feed_lease(profile), payload["updatedAt"])
    redis_client.expire(keys.feed_lease(profile), 120)
    redis_client.set(keys.feed_switch_state(), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return payload


def read_string(value) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
