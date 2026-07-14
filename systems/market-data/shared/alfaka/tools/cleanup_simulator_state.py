from __future__ import annotations

import argparse
import os
from collections.abc import Iterable

from alfaka.common.redis_keys import RedisKeyBuilder


DEFAULT_SIMULATOR_SYMBOLS = ("AMD", "IFF", "OKE")
DEFAULT_CANDLE_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M")


def cleanup_simulator_market_state(
    redis_client,
    *,
    symbols: Iterable[str] = DEFAULT_SIMULATOR_SYMBOLS,
    intervals: Iterable[str] = DEFAULT_CANDLE_INTERVALS,
    prefix: str | None = None,
) -> int:
    keys = RedisKeyBuilder(prefix)
    stale_keys: set[str] = set()

    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        if not symbol:
            continue
        stale_keys.update(
            {
                keys.live_trade(symbol),
                keys.live_quote(symbol),
                keys.live_event(symbol),
                keys.market_status_symbol_latest(symbol),
                keys.order_flow_minutes(symbol),
                keys.order_flow_live_minute(symbol),
            }
        )
        for raw_interval in intervals:
            interval = str(raw_interval).strip()
            if not interval:
                continue
            stale_keys.update(
                {
                    keys.candle_cache(symbol, interval),
                    keys.live_candle(symbol, interval),
                    keys.latest_closed_candle(symbol, interval),
                    keys.closed_candle_watermark(symbol, interval),
                }
            )
            stale_keys.update(redis_client.scan_iter(match=keys.key(f"state:candle-window:{symbol}:{interval}:*")))
            stale_keys.update(redis_client.scan_iter(match=keys.key(f"pending:replace:{symbol}:{interval}:*")))

    return int(redis_client.delete(*sorted(stale_keys))) if stale_keys else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove transient GOPS simulator market state from Redis.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SIMULATOR_SYMBOLS))
    parser.add_argument("--intervals", default=",".join(DEFAULT_CANDLE_INTERVALS))
    args = parser.parse_args()

    import redis

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = redis.from_url(redis_url, decode_responses=True)
    deleted = cleanup_simulator_market_state(
        client,
        symbols=args.symbols.split(","),
        intervals=args.intervals.split(","),
    )
    print(f"Removed {deleted} transient simulator Redis keys.")


if __name__ == "__main__":
    main()
