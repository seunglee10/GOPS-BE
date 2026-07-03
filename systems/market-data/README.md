# Market Data System

Owns Alpaca ingest, stream processing, market-data storage, backfill, and serving helpers.

For chart-data rebuild work, `docs/CHART_DATA_REBUILD_PLAN.md` is the source of
truth. Older notes in this system that describe a fixed preset universe,
S&P500-wide chart collection, broad initial preload, raw S3 replay as a normal
source, or non-Mermaid Kafka topic layouts are superseded.

## Folders

```text
pods/market-ingestor/       Alpaca live WebSocket entrypoint
pods/market-processor/      Python stream processor for local and current AWS runtime
pods/s3-sink/               processed and raw Kafka topics to S3
pods/clickhouse-loader/     processed Kafka topics to ClickHouse
pods/backfill-worker/       Redis queued historical backfill worker
jobs/symbol-registry-sync/  symbol metadata sync job
jobs/coverage-repair/       chart coverage audit/backfill queue job
jobs/initial-load/          chunked canonical history initial-load planner job
config/                     market universe and subscription policy
shared/alfaka/              market-data import namespace
tests/                      market-data tests
```

## Runtime Entrypoints

```text
pods/market-ingestor/market_stream.py           wraps alfaka.alpaca.websocket_collector
pods/market-processor/local_main.py             wraps alfaka.streaming.processor
infra/k8s/base/deployment-market-processor.yaml current Kubernetes processor deployment
pods/s3-sink/processed_sink.py                  wraps alfaka.storage.processed_s3_sink
pods/s3-sink/raw_archive_sink.py                 wraps alfaka.storage.raw_s3_archive_sink
pods/clickhouse-loader/processed_loader.py      wraps alfaka.storage.clickhouse_loader
pods/backfill-worker/main.py                    wraps alfaka.backfill.worker
jobs/symbol-registry-sync/main.py               wraps alfaka.tools.sync_symbol_registry
jobs/coverage-repair/main.py                    audits /api/charts/candles and queues /api/charts/backfill
jobs/initial-load/main.py                       plans/queues chunked initial_load jobs
```

## Images

```text
gops-market-ingestor    market-ingestor
gops-market-processor   market-processor, symbol-registry-sync, coverage-repair, initial-load
gops-market-storage     processed S3 sink, raw S3 archive, clickhouse-loader
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

Keep `alfaka.*` imports stable. Docker, compose, k8s, tests, and local scripts should place `systems/market-data/shared` on `PYTHONPATH`.

## Feed Profiles And Sessions

Live Alpaca ingest is profile-scoped. The default v1 runtime uses `sip` for `04:00-20:00 ET` (`pre`, `regular`, `after`) and `boats` for `20:00-04:00 ET` (`overnight`). `overnight` remains an alias for Alpaca's overnight feed where needed. Compose and k8s run separate ingestor runtimes per active profile with distinct client IDs instead of switching feeds inside one process.

Raw envelopes, normalized streaming events, Redis latest/live state, ClickHouse rows, API candles, and chart snapshots carry `feedProfile` and `marketSession`. The session model is `pre`, `regular`, `after`, and `overnight`; daily/weekly/monthly candle serving falls back to `regular` when historical rows lack stored session metadata. Existing ClickHouse volumes can add the columns in place, but true multi-feed row preservation requires a table rebuild using the feed/session-aware `ORDER BY` from `infra/clickhouse/initdb/01-market-data.sql`.

## On-Demand Chart Scope

The chart rebuild starts with no preloaded chart data. The runtime loads only
the `symbol + timeframe + range + layer` requested by the chart or an explicit
subscription. Symbol registry data may help validation/search, but it is not a
chart-data preload plan.

Runtime policy:

- no preset universe chart preload
- realtime trades/quotes/bars/events only for explicit active subscriptions
- Redis keeps newest 120 candles per `symbol + timeframe`
- older confirmed candles come from ClickHouse
- ClickHouse misses check S3 final/manifest before Alpaca historical
- raw S3 archives are backup-only and not an active read/materialization source

The ingestor should read the resolved tier state from Redis/control-plane keys, not hardcode symbol lists.

## Live Path Trace

Use this when realtime data appears absent in AWS or local compose. Trace one symbol and stop at the first broken hop.

Read-only helper:

```bash
PYTHONPATH=systems/market-data/shared python scripts/local/check-live-path.py NVDA --interval 1m
scripts/aws/check-live-path.sh NVDA
```

```bash
kubectl logs -n alfaka-market-data deploy/alfaka-alpaca-ingestor-sip --tail=100
kubectl logs -n alfaka-market-data deploy/alfaka-alpaca-ingestor-crypto --tail=100
kubectl logs -n alfaka-market-data deploy/alfaka-market-processor --tail=100
kubectl logs -n alfaka-market-data deploy/alfaka-clickhouse-loader --tail=100
```

Kafka must show the processor group consuming the raw topics with bounded lag:

```bash
kafka-consumer-groups.sh --bootstrap-server "$KAFKA_BOOTSTRAP_SERVERS" --describe --group alfaka-market-processor
```

Then verify the serving side uses the same namespace:

```bash
redis-cli -u "$REDIS_URL" GET "gops:market:on-demand:v1:live:candle:NVDA:1m"
curl -fsS "$GOPS_API_BASE_URL/api/charts/candles?symbol=NVDA&interval=1m&limit=20"
```

The required path is `Alpaca -> market.input.realtime.* Kafka -> Python processor feed guard -> Redis/market.layer.* Kafka -> ClickHouse/S3 final -> API/WebSocket -> browser`.
During closed market hours, use controlled replay and local live-path tracing for debugging. Keep a market-hours AWS smoke as an out-of-band production/live-market verification item unless the user explicitly reopens direct AWS verification.

## Coverage Repair

Use the repair job after bootstrapping a new local volume, restoring ClickHouse, or changing the watchlist:

```bash
docker compose --profile repair run --rm coverage-repair
```

The compose job is dry-run by default. To queue missing backfills:

```bash
COVERAGE_REPAIR_DRY_RUN=false docker compose --profile repair run --rm coverage-repair
```

The job talks to the API server rather than Redis or ClickHouse directly, so derived intervals keep the same source-interval rules as the frontend: `5m/10m` repair through `1m`, and `1W/1M` repair through `1D`.
Backfill API requests are queued in Redis Streams by default, with consumer-group claim/ack/reclaim semantics and dead-letter handling after the configured max attempts. Stale queued/running gapfill records fail after `BACKFILL_ACTIVE_STALE_SECONDS`, and oversized `1m` gapfill windows are rejected by `BACKFILL_MAX_GAPFILL_1M_RANGE_HOURS`; broad intraday rebuilds belong to Initial Load or explicit S3 materialize jobs.

## Initial Load

Initial Load is legacy/bootstrap tooling during the on-demand chart rebuild. Do
not use it as the normal chart path and do not run broad preset-universe preload
without explicit operator approval. Normal chart expansion is:

```text
Redis latest 120 -> ClickHouse -> S3 final/manifest -> Alpaca historical
```

Canonical historical candles use Alpaca `adjustment=split` and are stored as
`priceAdjustment=split`, `canonicalVersion=v2`; chart serving excludes
legacy/raw/unknown rows.

Before deleting or quarantining suspect ClickHouse candle rows, run `python -m alfaka.tools.canonical_candle_audit` with optional `CANONICAL_AUDIT_SYMBOL`, `CANONICAL_AUDIT_INTERVAL`, and `CANONICAL_AUDIT_LIMIT` to get duplicate/non-canonical/invalid OHLC row counts.
`force=true` backfill bypasses existing canonical S3 processed objects and fetches Alpaca again. This is required when a previously materialized canonical object is known to contain bad values. For `1D`, suspicious split-day high/low outliers are validated against same-day split-adjusted `1m` bars; only the outlier high/low is repaired, while daily open/close/volume remain from dailyBars.

Raw backup may be written as a side effect for audit, but missing raw backup must
not fail a chart request, backfill job, or ClickHouse materialization job.
Backfill/materialization decisions use Redis, ClickHouse, and S3 final/manifest,
not raw backup objects.

For local AWS-contract runs, set
`ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager` and keep `S3_PROCESSED_FORMAT=parquet`.
Docker Compose passes `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` through so an
explicit `ALPACA_CREDENTIAL_SOURCE=local-env` smoke can run while Secrets Manager
is disconnected. Keep legacy universes and `jsonl` output out of the on-demand
rebuild contract.

Drag-left chart history uses the candles API first. If the returned older range is partial but repairable, the frontend queues a bounded backfill request with an explicit `start`/`end`, polls `/api/charts/backfill/status`, and refetches the same range after completion. The chart request path must still serve from Redis/ClickHouse; it must not list S3 or call Alpaca synchronously. Do not convert a sparse chart window into a full-range `force=true` `1m` backfill.

Before any operator-approved bootstrap, prove S3-to-ClickHouse materialization
with one explicit final candle object:

```bash
S3_MATERIALIZE_KEYS=market-data/rebuild-20260702-lazy-v1/final/candles/.../canonical=v2.parquet python -m alfaka.storage.s3_materializer
```
