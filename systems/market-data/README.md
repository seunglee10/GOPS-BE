# Market Data System

Owns Alpaca ingest, stream processing, market-data storage, backfill, and serving helpers.

## Folders

```text
pods/market-ingestor/       Alpaca live WebSocket entrypoint
pods/market-processor/      Python stream processor for local and current AWS runtime
pods/s3-sink/               processed and raw Kafka topics to S3
pods/clickhouse-loader/     processed Kafka topics to ClickHouse
pods/backfill-worker/       Redis queued historical backfill worker
pods/market-processor/flink/ future Flink job contract for market processing
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

## Universe And Trade Tiers

The v1 clean-rebuild collection universe is the 20-symbol `gops20` set in `config/market-data-request.json` and selected by `ALPACA_UNIVERSE=gops20`.

Runtime policy:

- full universe: `bars`, `updatedBars`, `dailyBars`, `statuses`
- trade tier: active chart symbols + watchlist symbols + Hot Ranking symbols
- hot tier: Top10 by current-session dollar volume inside the 20-symbol universe, exposed to the frontend through `GET /api/charts/hot-symbols`

The ingestor should read the resolved tier state from Redis/control-plane keys, not hardcode symbol lists.
The Hot Ranking serving path should read a Redis snapshot first, then use a single ClickHouse dollar-volume aggregate query, and only fall back to per-symbol scans when neither source is available.

## Live Path Trace

Use this when realtime data appears absent in AWS or local compose. Trace one symbol and stop at the first broken hop.

Read-only helper:

```bash
PYTHONPATH=systems/market-data/shared python scripts/local/check-live-path.py NVDA --interval 1m
scripts/aws/check-live-path.sh NVDA
```

```bash
kubectl logs -n alfaka-market-data deploy/alfaka-alpaca-ingestor --tail=100
kubectl logs -n alfaka-market-data deploy/alfaka-market-processor --tail=100
kubectl logs -n alfaka-market-data deploy/alfaka-clickhouse-loader --tail=100
```

Kafka must show the processor group consuming the raw topics with bounded lag:

```bash
kafka-consumer-groups.sh --bootstrap-server "$KAFKA_BOOTSTRAP_SERVERS" --describe --group alfaka-market-processor
```

Then verify the serving side uses the same namespace:

```bash
redis-cli -u "$REDIS_URL" GET "candle:NVDA:1m:live"
curl -fsS "$GOPS_API_BASE_URL/api/charts/candles?symbol=NVDA&interval=1m&limit=20"
```

The required path is `Alpaca -> raw Kafka -> Python processor -> Redis/processed Kafka -> ClickHouse -> API/WebSocket -> browser`.
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

Use the initial-load job to plan or queue broad canonical history loads as bounded Redis Streams jobs. It is dry-run by default, uses the explicit 20-symbol `INITIAL_LOAD_SYMBOLS` list for the clean rebuild, and only supports canonical source intervals `1m` and `1D`. Canonical historical candles use Alpaca `adjustment=split` and are stored as `priceAdjustment=split`, `canonicalVersion=v2`; chart serving excludes legacy/raw/unknown rows.

Before deleting or quarantining suspect ClickHouse candle rows, run `python -m alfaka.tools.canonical_candle_audit` with optional `CANONICAL_AUDIT_SYMBOL`, `CANONICAL_AUDIT_INTERVAL`, and `CANONICAL_AUDIT_LIMIT` to get duplicate/non-canonical/invalid OHLC row counts.
`force=true` backfill bypasses existing canonical S3 processed objects and fetches Alpaca again. This is required when a previously materialized canonical object is known to contain bad values. For `1D`, suspicious split-day high/low outliers are validated against same-day split-adjusted `1m` bars; only the outlier high/low is repaired, while daily open/close/volume remain from dailyBars.

For the 20-symbol bootstrap, run the 3-year `1D` range first, then run `1m` in reviewed windows: recent 3 months, recent 1 year, then full 3 years. The v1 `1m` preload lower bound is fixed at `BACKFILL_INITIAL_LOAD_1M_MIN_START=2023-07-01T00:00:00Z`. Use `S3_PROCESSED_FORMAT=parquet`, `S3_HISTORICAL_RAW_PARTITION_MODE=chunk`, and `S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT=compact` for broad preload. Re-running the same plan is safe: existing queued/running/succeeded chunk requests are skipped and do not consume enqueue capacity.
Unlike GapFill, Initial Load does not treat ClickHouse coverage alone as success. It still creates S3 raw/processed evidence unless the exact chunk request already exists or the operator explicitly runs an S3-only replay mode.
Chunks with no Alpaca bars complete as `alpaca-empty` and write an empty marker under `S3_MANIFEST_PREFIX/empty/candles/...`, allowing resume to avoid repeated calls for pre-listing or inactive historical ranges.

For local AWS-contract runs, market-data Docker services pin `ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager` and `S3_PROCESSED_FORMAT=parquet`. This prevents stale root `.env` values such as legacy universes, local Alpaca keys, or `jsonl` output from overriding the gops20/Secrets Manager/parquet contract.

Intraday chart renderability only treats sparse gaps as blocking when both neighboring candles are inside the configured regular market session. Sparse after-hours 1m bars are allowed to render because Alpaca may not emit a bar for inactive extended-hours minutes.

Drag-left chart history uses the candles API first. If the returned older range is partial but repairable, the frontend queues a bounded backfill request with an explicit `start`/`end`, polls `/api/charts/backfill/status`, and refetches the same range after completion. The chart request path must still serve from Redis/ClickHouse; it must not list S3 or call Alpaca synchronously. Do not convert a sparse chart window into a full-range `force=true` `1m` backfill.

```bash
INITIAL_LOAD_START=2023-06-30T00:00:00Z INITIAL_LOAD_END=2026-06-30T00:00:00Z docker compose --profile repair run --rm initial-load
```

To enqueue jobs after reviewing the dry-run output:

```bash
INITIAL_LOAD_DRY_RUN=false INITIAL_LOAD_START=2023-06-30T00:00:00Z INITIAL_LOAD_END=2026-06-30T00:00:00Z docker compose --profile repair run --rm initial-load
```

For `1m`, always pass the interval explicitly and prefer month-sized reviewed windows. Do not run `1m` windows earlier than `2023-07-01T00:00:00Z`. Dry-run row estimates use `HISTORICAL_1M_MINUTES_PER_TRADING_DAY=960` by default because Alpaca historical 1m bars may include extended-hours data:

```bash
INITIAL_LOAD_INTERVALS=1m INITIAL_LOAD_START=2026-05-01T00:00:00Z INITIAL_LOAD_END=2026-06-01T00:00:00Z docker compose --profile repair run --rm initial-load
```

Before broad preload, prove S3-to-ClickHouse materialization with one explicit processed candle object:

```bash
S3_MATERIALIZE_KEYS=market-data/rebuild-20260701/final/candles/.../canonical=v2.parquet python -m alfaka.storage.s3_materializer
```
