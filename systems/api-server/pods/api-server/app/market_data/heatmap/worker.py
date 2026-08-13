"""Background warmer for the Redis-backed market heatmap projection."""

from __future__ import annotations

import os
import time
import uuid

from app.market_data.heatmap.service import (
    DEFAULT_HEATMAP_UNIVERSE,
    get_heatmap_service,
    heatmap_cache_key,
)
from app.services.alfaka_market_data import get_market_data_provider


RELEASE_OWNED_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def bool_env(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def warm_once(service, universe: str) -> bool:
    redis_client = service._redis()
    if redis_client is None:
        service.rebuild(universe)
        return True

    lock_key = f"{heatmap_cache_key(universe)}:build-lock"
    lock_ttl = int_env("HEATMAP_PROJECTION_LOCK_SECONDS", 90)
    lock_token = uuid.uuid4().hex
    try:
        acquired = redis_client.set(lock_key, lock_token, nx=True, ex=lock_ttl)
    except TypeError:
        acquired = redis_client.set(lock_key, lock_token, ex=lock_ttl, nx=True)
    if not acquired:
        return False
    try:
        service.rebuild(universe)
        return True
    finally:
        try:
            redis_client.eval(RELEASE_OWNED_LOCK_SCRIPT, 1, lock_key, lock_token)
        except Exception:
            # Redis expires the lock automatically. Avoid a non-atomic DELETE,
            # which could remove a lock acquired by a newer worker.
            pass


def main() -> None:
    if not bool_env("HEATMAP_PROJECTION_ENABLED", True):
        print("Heatmap projection worker disabled: HEATMAP_PROJECTION_ENABLED=false", flush=True)
        return

    universe = (os.getenv("HEATMAP_UNIVERSE") or DEFAULT_HEATMAP_UNIVERSE).strip().lower()
    interval = int_env("HEATMAP_PROJECTION_INTERVAL_SECONDS", 60)
    service = get_heatmap_service(get_market_data_provider())
    print(f"Heatmap projection worker 시작: universe={universe} interval={interval}s", flush=True)

    while True:
        started = time.monotonic()
        try:
            warm_once(service, universe)
        except Exception as exc:
            print(f"Heatmap projection 실패: {exc.__class__.__name__}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, interval - elapsed))


if __name__ == "__main__":
    main()
