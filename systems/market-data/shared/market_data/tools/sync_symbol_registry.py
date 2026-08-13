import json
import os
import sys

import redis

from market_data.alpaca.assets import fetch_alpaca_assets
from market_data.common.env import load_dotenv
from market_data.common.kafka_io import create_json_producer
from market_data.common.redis_keys import RedisKeyBuilder
from market_data.common.secrets import load_alpaca_credentials
from market_data.storage.clickhouse_loader import ClickHouseHttpClient, symbol_to_clickhouse_row


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

    enriched = sync_postgres_instruments(metadata)
    cache_to_redis(enriched)
    publish_instrument_mappings(enriched)


def sync_postgres_instruments(metadata):
    conninfo = os.getenv("DATABASE_URL")
    if not conninfo:
        required = ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
        if not all(os.getenv(name) for name in required):
            return [{**item, "instrument_id": canonical_instrument_id(item["symbol"])} for item in metadata]
        from psycopg.conninfo import make_conninfo
        conninfo = make_conninfo(
            host=os.environ["DATABASE_HOST"], port=os.getenv("DATABASE_PORT", "5432"),
            dbname=os.environ["DATABASE_NAME"], user=os.environ["DATABASE_USER"],
            password=os.environ["DATABASE_PASSWORD"],
        )
    import psycopg

    enriched = []
    with psycopg.connect(conninfo) as conn:
        for item in metadata:
            row = conn.execute(
                "SELECT gops_ensure_instrument(%s, %s, %s, 'alpaca')",
                (item["symbol"], item.get("market"), item.get("exchange")),
            ).fetchone()
            enriched.append({**item, "instrument_id": str(row[0])})
    print(f"PostgreSQL instruments/aliases 갱신 완료: rows={len(enriched)}", flush=True)
    return enriched


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
        instrument_id = item.get("instrument_id")
        if instrument_id:
            redis_client.setex(f"gops:v2:instrument-alias:alpaca:{item['symbol'].upper()}", ttl, instrument_id)
            redis_client.setex(
                f"gops:v2:instrument:{instrument_id}", ttl,
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            )
    print(f"Redis symbol metadata cache 갱신 완료: rows={len(metadata)}", flush=True)


def publish_instrument_mappings(metadata):
    if os.getenv("SYMBOL_REGISTRY_KAFKA_PUBLISH", "true").lower() in {"0", "false", "no"}:
        return
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    if not servers:
        return
    producer = create_json_producer(servers, "gops-instrument-registry-sync")
    topic = os.getenv("INSTRUMENT_REGISTRY_TOPIC", "reference.instruments.v1")
    for item in metadata:
        producer.send(topic, key=str(item["instrument_id"]), value={
            "schema_version": "instrument-reference.v1",
            "instrument_id": item["instrument_id"],
            "provider": "alpaca",
            "provider_symbol": item["symbol"],
            "canonical_symbol": str(item["symbol"]).upper().replace("-", "."),
            "market": item.get("market"),
            "exchange": item.get("exchange"),
            "status": item.get("status"),
        })
    producer.flush()
    producer.close()
    print(f"Kafka instrument mapping 발행 완료: rows={len(metadata)} topic={topic}", flush=True)


def canonical_instrument_id(symbol):
    from market_data.storage.clickhouse_loader import canonical_instrument_id as resolve
    return resolve(symbol)


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
