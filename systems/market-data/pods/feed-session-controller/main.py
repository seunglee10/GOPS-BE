import os
import time

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.runtime_config import validate_required_values
from alfaka.common.runtime_health import write_component_health
from alfaka.realtime.feed_control import reconcile_active_feed


def main():
    load_dotenv()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    poll_seconds = positive_float(os.getenv("FEED_SESSION_POLL_SECONDS", "15"), 15.0)
    validate_required_values("feed session controller", {"redis_url": redis_url})
    redis_client = redis.from_url(redis_url, decode_responses=True)
    keys = RedisKeyBuilder()
    while True:
        payload = reconcile_active_feed(redis_client, keys=keys)
        write_component_health(
            redis_client,
            keys,
            "feed-session-controller",
            status="ok",
            activeFeedProfile=payload["activeFeedProfile"],
            feedEpoch=payload["epoch"],
            marketSession=payload["marketSession"],
        )
        print(f"feed-session-controller active={payload}", flush=True)
        time.sleep(poll_seconds)


def positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


if __name__ == "__main__":
    main()
