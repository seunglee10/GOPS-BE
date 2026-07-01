# Market Data System

Owns Alpaca ingest, stream processing, market-data storage, backfill, and serving helpers.

## Folders

```text
pods/market-ingestor/       Alpaca live WebSocket entrypoint
pods/market-processor/      Python stream processor for local and current AWS runtime
pods/clickhouse-loader/     processed Kafka topics to ClickHouse
pods/backfill-worker/       Redis queued historical backfill worker
pods/market-processor/flink/ future Flink job contract for market processing
jobs/symbol-registry-sync/  symbol metadata sync job
config/                     market universe and subscription policy
shared/alfaka/              market-data import namespace
tests/                      market-data tests
```

## Runtime Entrypoints

```text
pods/market-ingestor/market_stream.py           wraps alfaka.alpaca.websocket_collector
pods/market-processor/local_main.py             wraps alfaka.streaming.processor
infra/k8s/base/deployment-market-processor.yaml current Kubernetes processor deployment
pods/clickhouse-loader/processed_loader.py      wraps alfaka.storage.clickhouse_loader
pods/backfill-worker/main.py                    wraps alfaka.backfill.worker
jobs/symbol-registry-sync/main.py               wraps alfaka.tools.sync_symbol_registry
```

## Images

```text
gops-market-ingestor    market-ingestor
gops-market-processor   market-processor, symbol-registry-sync
gops-market-storage     clickhouse-loader and post-insert S3 archive utilities
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

Live Alpaca ingest is profile-scoped. The supported v1 profiles are `sip`, `iex`, and `boats`; `overnight` is an alias for the BOATS profile where needed. The default compose and k8s runtime starts only the SIP ingestor. IEX and BOATS are optional extra runtimes with distinct client IDs and should be enabled only when the Alpaca account allows additional WebSocket feed connections. The optional k8s manifest lives at `infra/k8s/optional/deployment-alpaca-ingestor-extra-feeds.yaml`.

Raw envelopes, normalized streaming events, Redis latest/live state, ClickHouse rows, API candles, and chart snapshots carry `feedProfile` and `marketSession`. The session model is `pre`, `regular`, `after`, and `overnight`; daily/weekly/monthly candle serving falls back to `regular` when historical rows lack stored session metadata. Existing ClickHouse volumes can add the columns in place, but true multi-feed row preservation requires a table rebuild using the feed/session-aware `ORDER BY` from `infra/clickhouse/initdb/01-market-data.sql`.

## Universe And Trade Tiers

The v1 collection universe is S&P 500, stored in `config/sp500-universe.json` and selected by `ALPACA_UNIVERSE=sp500`.

Runtime policy:

- full universe: `bars`, `updatedBars`, `dailyBars`, `statuses`
- trade tier: active chart symbols + watchlist symbols + Hot Ranking symbols
- hot tier: top 10 by current-session dollar volume, exposed to the frontend through `GET /api/charts/hot-symbols`

The ingestor should read the resolved tier state from Redis/control-plane keys, not hardcode symbol lists.
The Hot Ranking serving path should read a fresh Redis snapshot first, then use a single ClickHouse dollar-volume aggregate query. If ClickHouse cannot fill the requested Top N, the API completes the ranking from the configured universe with per-symbol fallback records before persisting `hot:symbols` and `hot:symbols:snapshot`.

## Live Path Trace

Use this when realtime data appears absent in AWS or local compose. Trace one symbol and stop at the first broken hop.

Read-only helper:

```bash
PYTHONPATH=systems/market-data/shared python scripts/local/check-live-path.py AAPL --interval 1m
scripts/aws/check-live-path.sh AAPL
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
redis-cli -u "$REDIS_URL" GET "candle:AAPL:1m:live"
curl -fsS "$GOPS_API_BASE_URL/api/charts/candles?symbol=AAPL&interval=1m&limit=20"
```

The required path is `Alpaca -> raw Kafka -> Python processor -> Redis/processed Kafka -> ClickHouse -> API/WebSocket -> browser`.
During closed market hours, use controlled replay and local live-path tracing for debugging. Keep a market-hours AWS smoke as an out-of-band production/live-market verification item unless the user explicitly reopens direct AWS verification.

## Range Backfill

Chart history loading is driven by the user-visible range. The initial chart snapshot can ask `/api/charts/candles` for the latest `limit`; once the user pans or zooms into history, the frontend asks the same candles API for the visible range plus buffer with half-open `from`/`to` query parameters. When that visible or adjacent range is missing, it queues an explicit half-open `start`/`end` request (`[start, end)`):

```bash
curl -fsS -X POST "$GOPS_API_BASE_URL/api/charts/backfill" \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"AAPL","interval":"1m","start":"2026-06-29T13:30:00Z","end":"2026-06-29T20:00:00Z"}'
```

The worker checks ClickHouse first, fetches only missing source buckets from Alpaca, materializes them directly to ClickHouse in `event_time` month-partition chunks, then best-effort archives the same processed candles to S3 under `market-data/dev/helixho/backfill/processed`. `5m` and `10m` requests backfill `1m` source candles; `1W` and `1M` requests backfill `1D` source candles. Daily source candles use canonical UTC date midnight (`YYYY-MM-DDT00:00:00.000Z`) for live ingestion, historical backfill, coverage checks, and gap detection. Historical Alpaca backfill uses `HISTORICAL_ADJUSTMENT=split` by default, and both serving and backfill clamp requests to `MARKET_DATA_MAX_HISTORY_YEARS` so stale local rows older than the subscription window are not rendered or requested. Intraday frontend backfill windows are calculated in regular-session market minutes rather than wall-clock minutes, and mirror the default NYSE holiday calendar, so dragging left from a market open requests prior-session candles instead of only the overnight or holiday gap. The frontend requests the visible range plus an interval-specific buffer, with a larger buffer for `1m`, `5m`, `10m`, and `1D` to reduce pan jitter while still staying tied to the user's visible range; after a repair succeeds it refetches that same `from`/`to` range instead of falling back to a latest-only page. It does not ask for a fixed multi-week preload before the user pans there. Intraday Alpaca fetch ranges are coalesced up to `BACKFILL_INTRADAY_FETCH_MAX_DAYS` so missing sessions are fetched in page-sized chunks instead of one request per session. Historical Alpaca fetches use the page-sized `HISTORICAL_LIMIT` default of `10000` and follow `next_page_token`; range jobs stay symbol-scoped so a broad S&P 500 operation cannot fill only the first symbols in one oversized multi-symbol response. Chart candle reads use the same half-open range contract as gap detection, so the `end` bucket is not returned again on adjacent range loads. Weekly and monthly chart reads aggregate stored `1D` rows, but apply the requested `from`/`to` window to the weekly or monthly bucket timestamp rather than cutting the underlying daily rows; this prevents partial first or last higher-timeframe candles when a caller passes a non-boundary timestamp. If Alpaca returns no bars, or only returns a later leading-edge partial history for a range that should contain market buckets, the worker records a no-data boundary so the chart stops retrying that unavailable range. A `force=true` backfill bypasses ClickHouse coverage and gap-scan skips, then refetches the whole clamped range; use it for vendor corrections or adjustment-mode repairs.

Local runtime smoke for this path:

```bash
SMOKE_BUILD=0 SMOKE_START=2026-06-24T13:30:00.000Z SMOKE_END=2026-06-24T14:00:00.000Z \
  bash scripts/local/smoke-backfill-missing-data.sh AAPL 1m

SMOKE_BUILD=0 SMOKE_START=2023-09-01T00:00:00.000Z SMOKE_END=2023-09-08T00:00:00.000Z \
  bash scripts/local/smoke-backfill-missing-data.sh AAPL 1D
```

The smoke accepts `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`. It records the exact backfill `requestId`, checks Redis status/queue access, verifies the requested ClickHouse source range before and after the request, and only treats S3 as a conditional archive check. Derived intervals are verified against their stored source interval (`5m`/`10m` use `1m`; `1W`/`1M` use `1D`). A range that was already covered can pass as a ClickHouse skip; use `force=true` or an older uncovered range when the goal is to prove a fresh Alpaca fetch.

Existing development volumes that contain legacy `1D` rows at New York midnight offsets can be checked with:

```bash
PYTHONPATH=systems/market-data/shared python -m alfaka.tools.repair_daily_candle_timestamps
```

Run the same command with `--apply --wait` only after reviewing the dry-run count. The tool inserts canonical UTC-midnight rows and removes only legacy non-midnight `1D` rows.

Backfill API requests are queued in Redis Streams with consumer-group claim/ack/reclaim semantics and dead-letter handling after the configured max attempts.

For local AWS-contract runs, market-data Docker services pin `ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager`. This keeps local env overrides from bypassing the S&P 500/Secrets Manager contract.

Intraday chart renderability only treats sparse gaps as blocking when both neighboring candles are inside the configured regular market session. Sparse after-hours 1m bars are allowed to render because Alpaca may not emit a bar for inactive extended-hours minutes.

Drag-left chart history uses the candles API first with `from`/`to` bounds for the buffered range. If that requested prior range is partial but repairable, the frontend queues a bounded backfill request with an explicit `start`/`end`, polls `/api/charts/backfill/status`, and refetches the same range after completion. The chart request path serves from Redis/ClickHouse; online backfill/gapfill fetches only the missing Alpaca range, writes directly to ClickHouse, and treats S3 as optional post-write archive. Quote UI updates for Watch List and Hot Ranking use `/ws/quotes` with `maxHz`; `/ws/quotes` reads Redis live/closed candle state only. Watch List persistence, Hot Ranking recomputation, and active chart WebSocket sessions write the Redis tier keys that the Alpaca ingestor polls for realtime trade subscriptions without persisting raw trade ticks to ClickHouse.

The ClickHouse loader may archive accepted realtime closed candles to S3 after insert success. It buffers that archive by row count/time (`S3_CLICKHOUSE_ARCHIVE_FLUSH_ROWS`, `S3_CLICKHOUSE_ARCHIVE_FLUSH_SECONDS`) so runtime archive evidence does not become one object per candle. There is no separate Kafka-to-S3 sink.
