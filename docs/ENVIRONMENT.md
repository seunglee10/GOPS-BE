# GOPS Environment And Platform Contracts

This file documents active environment and platform contracts.
Do not put real secrets here.

For the chart-data rewrite, the source of truth is:

```text
docs/CHART_DATA_REBUILD_PLAN.md
```

## Platform Folders

```text
platform/kafka/
platform/flink/
platform/redis/
platform/postgres/
platform/clickhouse/
platform/s3/
platform/secrets/
```

## Chart Data Rebuild Env

The next chart runtime starts from empty chart storage and fills requested data
on demand.

```text
REDIS_KEY_PREFIX=gops:market:on-demand:v1

S3_BUCKET=gops-market-data-<aws-account-id>-ap-northeast-2-an
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_LIVE_PREFIX=market-data/rebuild-20260702-lazy-v1/live
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final

CLICKHOUSE_DATABASE=market_data
```

`S3_RAW_PREFIX` is backup-only. Active chart serving, coverage checks, backfill
decisions, and ClickHouse materialization must use ClickHouse, S3 final objects,
and S3 manifests instead of raw payload archives.

Chart collection policy:

```text
No universe preload.
No fake candles.
Only the current chart or explicit subscription opens realtime collection.
Only missing requested ranges are backfilled.
```

## Alpaca Feeds

Realtime feed ownership is exclusive and uses `America/New_York`.

```text
ALPACA_FEED_PROFILES=sip,boats
ALPACA_ENFORCE_FEED_SESSION_WINDOW=true
ALPACA_SESSION_IDLE_POLL_SECONDS=60
ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager
ALPACA_SECRET_NAME=dev/alpaca
```

Feed session contract:

```text
04:00 - 20:00 ET = SIP only
20:00 - 04:00 ET = BOATS only
```

All market-data payloads must preserve:

```text
feedProfile
marketSession
feedEpoch
ingestorId
subscriptionSetVersion
```

Wrong-feed or stale-epoch payloads must be quarantined, not written to Redis,
ClickHouse, S3 final data, or WebSocket clients.

## Kafka

Current local stage:

```text
docker-compose kafka
docker-compose kafka-init
platform/kafka/topics.txt
```

Planned chart topics are listed in `platform/kafka/README.md` and
`docs/CHART_DATA_REBUILD_PLAN.md`. The ordering rule is always:

```text
Kafka key = symbol
Same symbol -> same partition
One partition -> one consumer pod in a consumer group
```

## Redis

```text
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=gops:market:on-demand:v1
```

Redis is not historical storage. It owns:

- latest 120 candles per `symbol + timeframe`;
- current provisional candle state;
- latest confirmed candle state;
- aggregation window state;
- pending replacement state;
- backfill stream, lock, status, and dead-letter state;
- exclusive SIP/BOATS feed control keys.

Run Redis so market-data writes keep working even if background snapshots fail:

```text
redis-server --appendonly yes --save "" --stop-writes-on-bgsave-error no
```

Do not use `FLUSHALL` for chart resets. Scan-delete only the chart keyspace
documented in `docs/CHART_DATA_REBUILD_PLAN.md`.

GOPS login sessions may also use Redis:

```text
AUTH_ENABLED=false
AUTH_REDIS_URL=
AUTH_REDIS_KEY_PREFIX=gops:auth
AUTH_SESSION_TTL_SECONDS=28800
AUTH_OAUTH_STATE_TTL_SECONDS=300
```

## ClickHouse

```text
CLICKHOUSE_HTTP_URL=http://localhost:8123
CLICKHOUSE_DATABASE=market_data
CLICKHOUSE_USER=alfaka
CLICKHOUSE_PASSWORD=alfaka
CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS=true
CLICKHOUSE_ENSURE_SESSION_COLUMNS=true
CLICKHOUSE_REQUIRE_CANONICAL_CANDLES=true
```

ClickHouse is the confirmed historical serving source for chart ranges older
than the Redis 120-candle cache.

Planned chart tables:

```text
market_data.chart_candles
market_data.trade_ticks
market_data.quote_ticks
market_data.market_events
market_data.market_status_events
market_data.volume_profile_bins_1m
market_data.load_audit
market_data.backfill_jobs
market_data.storage_object_audit
```

## S3

Leave endpoint values empty for real AWS S3.

```text
S3_ENDPOINT_URL=
DOCKER_S3_ENDPOINT_URL=
S3_PROCESSED_FORMAT=parquet
S3_HISTORICAL_RAW_PARTITION_MODE=chunk
S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT=compact
S3_FLUSH_COUNT=1
S3_FLUSH_INTERVAL_SECONDS=60
S3_RAW_FLUSH_COUNT=500
S3_RAW_FLUSH_INTERVAL_SECONDS=60
S3_PUT_MAX_ATTEMPTS=3
S3_PUT_RETRY_SLEEP_SECONDS=1
```

S3 final objects and manifests are durable evidence and ClickHouse rebuild
source. S3 raw objects are backup-only and are not queried synchronously or
asynchronously by active chart logic.

## API Server

The backend imports market/order shared packages by namespace:

```text
alfaka.*
kis_trader.*
```

Chart routes are preserved:

```text
GET  /api/charts/candles
POST /api/charts/backfill
GET  /api/charts/backfill/status
GET  /api/charts/symbols
WS   /ws/charts
```

Planned monitor routes are documented in `docs/CHART_DATA_REBUILD_PLAN.md`.

## KIS Demo Orders

Order runtime remains demo-only for v1:

```text
KIS_ENV=demo
KIS_CREDENTIAL_SOURCE=aws-secrets-manager
KIS_SECRET_NAME=tead/gops/kis
KIS_DEMO_APP_KEY
KIS_DEMO_APP_SECRET
KIS_DEMO_ACCOUNT_NO
KIS_ACCOUNT_PRODUCT_CODE=01
KIS_TOKEN_CACHE_PATH
KIS_TIMEOUT_SECONDS
KIS_BROKER_ADAPTER_ARGS
```

`KIS_ENV=real` remains disabled unless a future task explicitly changes the
order contract.
