import json
import os
import sys

import redis

from alfaka.alpaca.assets import fetch_alpaca_assets
from alfaka.common.env import load_dotenv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.secrets import load_alpaca_credentials
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, symbol_to_clickhouse_row


def main():
    load_dotenv()
    alpaca_key, alpaca_secret = load_alpaca_credentials()
    if not alpaca_key or not alpaca_secret:
        print("Alpaca 키가 없습니다. .env 또는 AWS Secrets Manager 설정을 넣어주세요.", file=sys.stderr)
        sys.exit(1)

    limit = parse_optional_int(os.getenv("SYMBOL_REGISTRY_LIMIT"))
    metadata = fetch_alpaca_assets(
        alpaca_key,
        alpaca_secret,
        base_url=os.getenv("ALPACA_TRADING_BASE_URL"),
        asset_class=os.getenv("SYMBOL_REGISTRY_ASSET_CLASS", "us_equity"),
        status=os.getenv("SYMBOL_REGISTRY_STATUS", "active"),
        limit=limit,
    )
    if not metadata:
        print("Alpaca assets 응답에 적재할 symbol metadata가 없습니다.", file=sys.stderr)
        sys.exit(1)

    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    rows = [symbol_to_clickhouse_row(item) for item in metadata]
    client.insert_json_each_row("symbols", rows)
    print(f"ClickHouse symbols 적재 완료: rows={len(rows)}", flush=True)

    cache_to_redis(metadata)


def cache_to_redis(metadata):
    redis_url = os.getenv("REDIS_URL")
    if not redis_url or os.getenv("SYMBOL_REGISTRY_REDIS_CACHE", "true").lower() in {"0", "false", "no"}:
        return

    redis_client = redis.from_url(redis_url, decode_responses=True)
    keys = RedisKeyBuilder()
    ttl = parse_optional_int(os.getenv("SYMBOL_REGISTRY_REDIS_TTL_SECONDS")) or 86400
    for item in metadata:
        redis_client.setex(
            keys.symbol_metadata(item["symbol"]),
            ttl,
            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        )
    print(f"Redis symbol metadata cache 갱신 완료: rows={len(metadata)}", flush=True)


def parse_optional_int(value):
    if not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


if __name__ == "__main__":
    main()
