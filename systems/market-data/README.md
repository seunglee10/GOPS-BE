# Market Data System

Owns Alpaca ingest, stream processing, market-data storage, on-demand fill, and serving helpers.

For chart-data work, `docs/CHART_DATA_ARCHITECTURE.md` is the current contract
and `docs/CHART_DATA_OPERATIONS.md` is the runbook. Exact storage and transport
details live in the platform READMEs.

## Folders

```text
pods/market-ingestor/       Alpaca live WebSocket entrypoint
pods/market-processor/      Python stream processor for local and current AWS runtime
pods/s3-sink/               processed and raw Kafka topics to S3
pods/clickhouse-loader/     processed Kafka topics to ClickHouse
pods/news-intelligence-worker/ precomputes Korean news summaries/relevance
jobs/symbol-registry-sync/  symbol metadata sync job
jobs/coverage-repair/       chart coverage and on-demand fill trace audit job
jobs/news-backfill/         Alpaca News historical raw/archive backfill job
jobs/news-intelligence-rebuild/ relevance v2 rebuild job for localized news
config/                     market universe and subscription policy
shared/alfaka/              market-data import namespace
tests/                      market-data tests
```

## Runtime Entrypoints

```text
pods/market-ingestor/market_stream.py           wraps alfaka.alpaca.websocket_collector
pods/market-processor/local_main.py             wraps alfaka.streaming.processor
infra/k8s/base/app/deployment-market-processor.yaml current Kubernetes processor deployment
pods/s3-sink/processed_sink.py                  wraps alfaka.storage.processed_s3_sink
pods/s3-sink/raw_archive_sink.py                 wraps alfaka.storage.raw_s3_archive_sink
pods/clickhouse-loader/processed_loader.py      wraps alfaka.storage.clickhouse_loader
pods/news-intelligence-worker/main.py           precomputes Korean news intelligence records
jobs/symbol-registry-sync/main.py               wraps alfaka.tools.sync_symbol_registry
jobs/coverage-repair/main.py                    audits /api/charts/candles fill traces
jobs/news-backfill/main.py                      stores Alpaca News raw payloads once per articleId
jobs/news-intelligence-rebuild/main.py          rebuilds relevance v2 fields for recent localized news
```

## Images

```text
gops-market-ingestor    market-ingestor
gops-market-processor   market-processor, symbol-registry-sync, coverage-repair
gops-market-storage     processed S3 sink, raw S3 archive, clickhouse-loader, news-intelligence-worker, news jobs
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

## Heatmap Projection Contract

The heatmap is a consumer of market-data state, not a fundamentals collector.
The API endpoint `GET /api/market/heatmap?universe=sp500` reads the
`systems/fundamentals` Redis/ClickHouse store, then combines
`sharesOutstanding` with the latest price/change rows from Redis/ClickHouse
serving state. Market-data services keep prices fresh; the fundamentals system
owns SEC ingestion, CIK mapping, and latest fundamentals persistence.

When fundamentals are absent, the API falls back to the local
`systems/market-data/config/sp500-heatmap-seed.json` layout seed. When prices are
absent, it can reuse the last cached projection or show the seed item with a
neutral/minimum market-cap fallback.

## Feed Profiles And Sessions

Live Alpaca ingest is profile-scoped. The default v1 runtime uses `sip` for `04:00-20:00 ET` (`pre`, `regular`, `after`) and `boats` for `20:00-04:00 ET` (`overnight`). `overnight` remains an alias for Alpaca's overnight feed where needed. Compose and k8s run separate ingestor runtimes per active profile with distinct client IDs instead of switching feeds inside one process.

Raw envelopes, normalized streaming events, Redis latest/live state, ClickHouse rows, API candles, and chart snapshots carry `feedProfile` and `marketSession`. The session model is `pre`, `regular`, `after`, and `overnight`; daily/weekly/monthly candle serving falls back to `regular` when historical rows lack stored session metadata. Existing ClickHouse volumes can add the columns in place, but true multi-feed row preservation requires a table rebuild using the feed/session-aware `ORDER BY` from `infra/clickhouse/initdb/01-market-data.sql`.

## Chart Scope

The chart path starts with no preloaded historical chart data. The API loads
only the `symbol + timeframe + range + layer` requested by the chart or an
explicit subscription. The SIP ingestor is allowed to keep an S&P500
`bars/updatedBars/dailyBars/statuses` baseline for fresh 1m entry, but that is
not a historical chart preload and does not include all-symbol `trades/quotes`.
Symbol registry data may help validation/search, but it is not a chart-data
preload plan.

Runtime policy:

- no preset historical universe chart preload
- SIP S&P500 baseline is bars/updatedBars/dailyBars/statuses only
- realtime trades/quotes only for explicit active subscriptions on the same SIP WebSocket
- Redis keeps only the frontend-requested recent chart window per `symbol + timeframe`
- older confirmed candles come from ClickHouse direct interval rows when present
- ClickHouse direct misses can fall back to query-time aggregation from `1m` or `1D`
- small incomplete foreground chart windows may use Alpaca REST direct bars for the requested interval, including intraday intervals enabled by `ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS`
- background misses check S3 final/manifest before Alpaca historical direct fill
- raw S3 archives keep only low-volume event/bar backup data, exclude realtime
  trades/quotes, and are not an active read/materialization source

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
curl -fsS "$GOPS_API_BASE_URL/api/monitor/market-data/realtime?symbol=NVDA&interval=1m"
```

The required path is `Alpaca -> market.input.realtime.* Kafka -> Python processor feed guard -> Redis live candle/trade/quote + market.layer.* Kafka -> ClickHouse/S3 final -> API/WebSocket -> browser`.
The chart page keeps the currently viewed symbol in the active realtime cohort
through `POST /api/charts/active-symbol`; that heartbeat is independent from the
visible interval and from whether the browser currently opens an intraday
WebSocket. The API WebSocket hub uses one global Redis `market.events` listener
and batched Redis live reads for active sessions; do not reintroduce one Redis
pubsub listener per symbol.
During closed market hours, use controlled replay and local live-path tracing for debugging. Keep a market-hours AWS smoke as an out-of-band production/live-market verification item unless the user explicitly reopens direct AWS verification.

## Coverage Repair

Use the repair job after bootstrapping a new local volume, restoring ClickHouse, or changing the watchlist:

```bash
docker compose --profile repair run --rm coverage-repair
```

The compose job is dry-run by default. It calls `GET /api/charts/candles`, so
the API may perform bounded on-demand fill for the requested window and then
report the resulting `fill` trace.

```bash
COVERAGE_REPAIR_DRY_RUN=false docker compose --profile repair run --rm coverage-repair
```

The job talks to the API server rather than Redis or ClickHouse directly, so it
uses the same serving rules as the frontend: realtime derived candles still use
local `1m`/`1D` aggregation, while historical repair uses Alpaca direct bars for
the requested interval.

## On-Demand Historical Fill

Normal chart expansion is:

```text
Redis recent requested window -> ClickHouse -> bounded auto/general foreground Alpaca REST direct
-> background S3 final/manifest -> background Alpaca historical direct
```

Canonical historical candles use Alpaca `adjustment=split` and are stored as
`priceAdjustment=split`, `canonicalVersion=v2`; chart serving excludes
legacy/raw/unknown rows.
Stored `1m` serving also accepts `priceAdjustment=live` closed realtime Alpaca
bars so current-session baseline rows are not hidden from the chart API. Daily
and historical canonical materialization remain `split` only.
Historical direct fill maps canonical intervals to Alpaca REST timeframes:
`1m=1Min`, `5m=5Min`, `10m=10Min`, `1h=1Hour`, `4h=4Hour`, `1D=1Day`,
`1W=1Week`, and `1M=1Month`.
For intraday equities, direct fill is split by market session before any Alpaca
REST call. `pre`, `regular`, and `after` slices use the configured historical
feed; `overnight` slices are BOATS live/on-demand only and appear as skipped
routes in `fill.feedRoutes` until the active chart subscription produces live
overnight candles.

Before deleting or quarantining suspect ClickHouse candle rows, run `python -m alfaka.tools.canonical_candle_audit` with optional `CANONICAL_AUDIT_SYMBOL`, `CANONICAL_AUDIT_INTERVAL`, and `CANONICAL_AUDIT_LIMIT` to get duplicate/non-canonical/invalid OHLC row counts.
Explicit operator repair may bypass existing canonical S3 processed objects and fetch Alpaca again when a previously materialized canonical object is known to contain bad values. For `1D`, suspicious split-day high/low outliers are validated against same-day split-adjusted `1m` bars; only the outlier high/low is repaired, while daily open/close/volume remain from dailyBars.

Raw backup may be written as a side effect for audit, but missing raw backup must
not fail a chart request, on-demand fill, or ClickHouse materialization job.
Fill/materialization decisions use Redis, ClickHouse, and S3 final/manifest,
not raw backup objects.

For local AWS-contract runs, set
`ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager` and keep `S3_PROCESSED_FORMAT=parquet`.
Docker Compose passes `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` through so an
explicit `ALPACA_CREDENTIAL_SOURCE=local-env` smoke can run while Secrets Manager
is disconnected. Keep legacy universes and `jsonl` output out of the on-demand
rebuild contract.

Chart entry and drag-left history use the candles API first. The frontend owns
the requested `interval`, `limit`, and `start`/`end` or `before` window. The API
checks Redis and ClickHouse first. If they are not renderable, the API may return
the partial/empty payload immediately and still queue background materialization
through S3 final/manifest and Alpaca historical for that requested window.
Set `ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED=true` only when small requests
should wait for direct Alpaca REST bars before responding. The response includes
`dataStatus`, `coverage`, and a `fill` trace that shows where
Redis/ClickHouse/S3/Alpaca hit, missed, timed out, or failed. Do not convert a
sparse chart window into a full-range preload.

Before any operator-approved bootstrap, prove S3-to-ClickHouse materialization
with one explicit final candle object:

```bash
S3_MATERIALIZE_KEYS=market-data/rebuild-20260702-lazy-v1/final/candles/.../canonical=v2.parquet python -m alfaka.storage.s3_materializer
```

## News Backfill And Hot Cache

The news path is precomputed. User news questions should read Redis/ClickHouse and should not call Alpaca or OpenAI synchronously.

```text
Alpaca News API
-> alpaca-news-ingestor / news-backfill
-> Kafka market.news.alpaca.v1
-> news-intelligence-worker
-> ClickHouse news_article_localizations
-> Redis news:v2:latest:ko:{symbol}
-> API/agent response
```

Storage boundaries:

- S3 keeps the Alpaca raw payload, preferably with `include_content=true`, as one canonical object per `articleId`.
- S3 symbol indexes point to canonical raw objects; they do not duplicate full article bodies per symbol.
- ClickHouse keeps recent app-serving rows, currently 30 days, with localized summaries, key points, relevance v2, sentiment, and links.
- Redis keeps the 30-day article hot cache (`news:v2:latest:*`, `news:v2:topic:*`) and 30 daily summaries per symbol. It stores localized summaries, links, relevance metadata, and daily coverage metadata, but not article body/raw payload.

The Kubernetes `alfaka-news-backfill`, `alfaka-news-intelligence-rebuild`, and
`alfaka-news-daily-summary-rebuild` Jobs are safe by default: they render with
dry-run env values. Set `NEWS_BACKFILL_DRY_RUN=false`,
`NEWS_INTELLIGENCE_REBUILD_DRY_RUN=false`, or
`NEWS_DAILY_SUMMARY_REBUILD_DRY_RUN=false` only for an intentional one-shot run
after reviewing scope and API/OpenAI cost. `news-intelligence-rebuild` also
warms Redis by default from the recent ClickHouse localization rows without
rewriting ClickHouse. Set `NEWS_INTELLIGENCE_REBUILD_REWRITE_CLICKHOUSE=true`
only for an intentional relevance-row rewrite maintenance run.
GitHub Actions dev/test deploys run the two news rebuild Jobs automatically
after a successful `market-storage` rollout so pushed Redis cache changes also
warm the existing 30-day ClickHouse news window.

Local small smoke:

```bash
NEWS_BACKFILL_SYMBOLS=AAPL,NVDA NEWS_BACKFILL_DAYS=2 NEWS_BACKFILL_CHUNK_DAYS=2 NEWS_BACKFILL_MAX_PAGES_PER_CHUNK=1 NEWS_BACKFILL_DRY_RUN=false docker compose --profile jobs --profile local-s3 run --rm news-backfill
```

AWS reviewed run:

```bash
AWS_ACCOUNT_ID=<aws-account-id> IMAGE_TAG=news-v2-smoke ./scripts/aws/build-and-push-images.sh market-storage
AWS_ACCOUNT_ID=<aws-account-id> IMAGE_TAG=news-v2-smoke NEWS_BACKFILL_DRY_RUN=false NEWS_BACKFILL_SYMBOLS=AAPL,NVDA NEWS_BACKFILL_DAYS=2 NEWS_BACKFILL_MAX_PAGES_PER_CHUNK=1 ./scripts/aws/run-news-backfill-job.sh
```

For the full S&P 500 1-year run, remove `NEWS_BACKFILL_SYMBOLS`, keep `NEWS_BACKFILL_UNIVERSE=sp500`, and run only after the smoke confirms S3, ClickHouse, and Redis counts.
For a long backfill, split the universe into deterministic shards with `NEWS_BACKFILL_SHARD_INDEX` and `NEWS_BACKFILL_SHARD_COUNT`; for example, run indexes `0..7` with count `8`.
