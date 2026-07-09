import os
import time

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.orderflow import pinned_symbols_from_env
from alfaka.realtime.subscription_cohorts import RealtimeSubscriptionCohortService
from alfaka.common.runtime_config import validate_required_values
from alfaka.common.runtime_health import write_component_health


def main():
    load_dotenv()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    poll_seconds = positive_float(os.getenv("SUBSCRIPTION_CONTROLLER_POLL_SECONDS", "5"), 5.0)
    validate_required_values("subscription controller", {"redis_url": redis_url})
    redis_client = redis.from_url(redis_url, decode_responses=True)
    keys = RedisKeyBuilder()
    cohorts = RealtimeSubscriptionCohortService(redis_client, keys, auto_reconcile=False)
    while True:
        cohorts.replace_order_flow_source(sorted(pinned_symbols_from_env()))
        records = cohorts.reconcile()
        symbols = sorted(redis_client.smembers(keys.subscription_symbols()))
        invalid_quote_symbols = [symbol for symbol in symbols if quote_without_trade(redis_client, keys, symbol)]
        for symbol in invalid_quote_symbols:
            redis_client.hset(keys.subscription_symbol(symbol), mapping={"enabled": "false", "disabledReason": "quotes-require-trades"})
        write_component_health(
            redis_client,
            keys,
            "subscription-controller",
            status="ok",
            subscriptionVersion=redis_client.get(keys.subscription_version()) or "0",
            symbolCount=len(symbols),
            sourceSymbolCount=len(records),
            invalidQuoteSymbols=",".join(invalid_quote_symbols),
        )
        print(f"subscription-controller symbols={symbols} invalidQuotes={invalid_quote_symbols}", flush=True)
        time.sleep(poll_seconds)


def quote_without_trade(redis_client, keys, symbol):
    record = redis_client.hgetall(keys.subscription_symbol(symbol)) or {}
    layers = {item for item in str(record.get("layers") or "").split(",") if item}
    return "quotes" in layers and "trades" not in layers


def positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


if __name__ == "__main__":
    main()
