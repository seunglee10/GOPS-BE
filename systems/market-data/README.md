# Market Data System

Owns Alpaca ingest, stream processing, market-data storage, backfill, and
serving helpers.

The chart-data rewrite source of truth is:

```text
../../docs/CHART_DATA_REBUILD_PLAN.md
```

## Current Direction

- Chart storage starts empty.
- No universe chart preload.
- No fake candles.
- Realtime is opened only for the current chart or explicit subscriptions.
- Missing requested historical ranges are filled on demand.
- Redis keeps only the latest 120 candles per `symbol + timeframe`.
- Older confirmed candles are served from ClickHouse.
- S3 final objects and manifests are durable evidence and ClickHouse rebuild
  source.
- S3 raw payload archives are backup-only and do not participate in chart
  serving, coverage, backfill decisions, or ClickHouse loading.
- SIP and BOATS are mutually exclusive active feeds.

## Folders

```text
pods/market-ingestor/       Alpaca live WebSocket entrypoint
pods/market-processor/      stream processor entrypoint
pods/s3-sink/               processed final data and raw backup payloads to S3
pods/clickhouse-loader/     processed Kafka topics to ClickHouse
pods/backfill-worker/       Redis queued backfill worker
pods/market-processor/flink/ staged future Flink contract
jobs/symbol-registry-sync/  symbol metadata sync job
jobs/coverage-repair/       chart coverage audit/backfill queue job
jobs/initial-load/          legacy bootstrap job; do not use for default chart preload
config/                     market subscription/config data
shared/alfaka/              market-data import namespace
tests/                      market-data tests
```

## Runtime Entrypoints

```text
pods/market-ingestor/market_stream.py
pods/market-processor/local_main.py
pods/s3-sink/processed_sink.py
pods/s3-sink/raw_archive_sink.py
pods/clickhouse-loader/processed_loader.py
pods/backfill-worker/main.py
jobs/symbol-registry-sync/main.py
jobs/coverage-repair/main.py
jobs/initial-load/main.py
```

## Images

```text
gops-market-ingestor    market-ingestor
gops-market-processor   market-processor, symbol-registry-sync, coverage-repair, initial-load
gops-market-storage     processed S3 sink, raw backup archive, clickhouse-loader
gops-backfill-worker    backfill-worker
```

## Platform Dependencies

```text
Kafka
Redis
ClickHouse
S3
Secrets Manager / Alpaca credentials
```

Keep `alfaka.*` imports stable. Docker, compose, k8s, tests, and local scripts
should place `systems/market-data/shared` on `PYTHONPATH`.

## Feed Contract

Use `America/New_York` session boundaries.

```text
04:00 - 20:00 ET = SIP only
20:00 - 04:00 ET = BOATS only
```

Each payload must carry:

```text
feedProfile
marketSession
feedEpoch
ingestorId
subscriptionSetVersion
```

Wrong-feed or stale-epoch payloads are quarantined and must not be written to
Redis, ClickHouse, S3 final data, or WebSocket clients.

## Chart Data Path

Read path:

```text
Frontend -> API -> Redis latest 120 -> ClickHouse -> backfill queue
```

Backfill path:

```text
Redis Stream -> backfill-worker -> S3 manifest -> Alpaca if missing -> S3 -> ClickHouse
```

Raw backup path:

```text
Alpaca payload -> raw S3 archive backup only
```

The raw backup path has no arrow back into chart API, coverage checks, backfill
decisions, or ClickHouse loaders.

Realtime path:

```text
Alpaca active feed -> Kafka key=symbol -> processors -> Redis live/cache -> WebSocket
```

Confirmed candle path:

```text
bars/updatedBars/dailyBars -> confirmation processor -> Redis replacement -> Kafka closed topic -> ClickHouse/S3
```

## Operational Rules

- Do not turn a chart request into a synchronous Alpaca call.
- Do not list S3 from the user-facing chart API path.
- Do not store provisional candles as canonical historical rows.
- Do not allow a single timestamp bucket to be appended twice.
- Do not use `initial-load` as default chart bootstrap.
- Do not use `FLUSHALL`; scan-delete only chart key patterns during reset.

## Useful Checks

```bash
PYTHONPATH=systems/market-data/shared python -m unittest discover systems/market-data/tests
```

```bash
redis-cli -u "$REDIS_URL" --scan --pattern 'gops:market:on-demand:v1:*'
```

```bash
curl -fsS "$GOPS_API_BASE_URL/api/charts/candles?symbol=AAPL&interval=1m&limit=120"
```
