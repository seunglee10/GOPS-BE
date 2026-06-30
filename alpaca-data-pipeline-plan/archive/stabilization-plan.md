# Alpaca Data Pipeline Stabilization Plan

Last updated: 2026-06-30
Status: Goal-mode implementation plan v1.24

## Purpose

AWS deployment exposed instability in the Alpaca market-data pipeline. This document is the living plan for stabilizing and then upgrading every Alpaca-driven path:

- ticks / trades
- 1 minute candles
- 5 minute candles
- 10 minute candles
- daily candles
- weekly candles
- monthly candles
- historical backfill
- realtime chart delivery
- S3 / ClickHouse / Redis serving projections

This is the implementation reference for the next Goal-mode stabilization effort. A Codex instance that does not know the prior conversation should be able to use this document to proceed milestone by milestone, verify each change, and avoid changing the agreed architecture by accident.

## Ground Rules

- Keep market-data ownership under `systems/market-data`.
- Keep API route contracts stable unless explicitly changed.
- Do not generate fake market candles.
- Preserve `alfaka.*` imports from `systems/market-data/shared`.
- Runtime, Docker, k8s, env, and AWS changes must update docs and platform contracts together.
- Treat `docs/`, `AGENTS.md`, and current code as source of truth.

## Goal-Mode Decision Snapshot

These are already decided for the v1 stabilization pass:

- AWS currently runs the stream processor as a Python process, not real Flink. The Python processor must be made explicit, observable, and contract-tested before any later Flink migration.
- Redis, ClickHouse, Kafka, and the processor runtime are pod-based in the current AWS shape; S3 is an external AWS bucket.
- Local runtime may connect to the same AWS S3 bucket when local S3 endpoints are empty. Broad local S3 preload is allowed only after implementation, automated tests, local runtime checks, and browser verification pass.
- Canonical historical candle sources are Alpaca `1m` bars and `1D` daily bars.
- Trades are for ticks, current price, volume profile, and provisional live candles. Full-universe historical trade storage is out of scope for v1.
- S&P 500 is the v1 full universe. Collect full-universe `bars`, `updatedBars`, `dailyBars`, and `statuses`.
- Trade collection is tiered: active chart symbols, user watchlists, and hot symbols.
- Frontend Watch List defaults are first-user suggestions only. The user-edited Watch List is the runtime state and must sync through `PUT /api/charts/watchlist` to Redis `watchlist:symbols`, which feeds the watchlist trade tier.
- Hot symbols are S&P 500 top 20 by current-session dollar volume. The Hot UI is a first-class `hotRanking` workspace panel backed by `GET /api/charts/hot-symbols`.
- Quote `changePercent` for Watch List, Hot Ranking, and chart quote headers means latest price versus previous regular-session close. It must not be computed from the current `1m` candle open, current-session first open, or visible chart range.
- If previous-close coverage is missing, quote `changePercent` should be absent/repair-needed rather than faked from intraday open. The repair path is canonical `1D` GapFill/Backfill, with previous-session `1m` close only as a bounded fallback when already stored.
- Store canonical `1m` and `1D` history; derive historical `5m`, `10m`, `1W`, and `1M` from deterministic latest-row source data.
- Every interval must have a realtime/provisional last candle using the `LIVE_CANDLE_UPDATE` contract.
- Redis is a hot realtime/cache layer, not the source-of-truth boundary for historical availability.
- Canonical `1D` historical preload target remains 3 years.
- Canonical `1m` historical preload target is scoped for v1: load monthly windows from the 2026-06 operating window back through 2025-04 inclusive, and do not fetch `1m` history starting before `2025-04-01T00:00:00Z`. The guard setting is `BACKFILL_INITIAL_LOAD_1M_MIN_START=2025-04-01T00:00:00Z`.
- Redis closed-candle caps are interval-specific: `1m=780`, `5m=156`, `10m=78`, `1D=756`, `1W=156`, `1M=36`.
- Backfill and GapFill are maintenance jobs. Chart requests should not synchronously scan S3 or call Alpaca.
- Near-term queue implementation target is Redis Streams with claim, ack, reclaim, retry, and dead-letter semantics. SQS remains a later AWS-managed alternative if the user explicitly changes direction.

## Goal-Mode Execution Contract

- Work milestone by milestone. Do not skip ahead when a milestone exit gate is failing.
- Each milestone must define and run automated tests that cover the behavior changed in that milestone.
- Each major user-facing milestone must include direct browser verification through the running app, not only API or unit tests.
- If browser verification or tests reveal a problem, create a focused subgoal for that failure and fix it before moving to the next milestone.
- Do not close the Goal until the final end-to-end browser pass and automated test suite pass.
- Do not use fake market candles in local runtime. Unit tests may use explicit fixtures, but local runtime and browser checks should use real stored data, controlled replay of real raw events, or clearly isolated UI test doubles.
- If live-market AWS verification is explicitly in scope while US markets are closed, do not claim the AWS realtime path is fixed solely from static checks. Either run a market-hours smoke test or document the market-hours smoke as an out-of-band verification item.
- If AWS access is blocked but the user explicitly asks to continue locally, proceed with local implementation using the AWS deployment contract as the target shape. Document which gates are locally verified versus AWS-unverified.
- If the user explicitly says not to use EKS and to proceed by assumption, do not spend implementation time on live EKS checks. Continue local milestones against the AWS deployment contract, mark EKS/AWS runtime trace as out-of-band and unverified, and only revisit it if the user reopens AWS verification.
- Current run direction: do not use EKS. Goal closure should be based on local AWS-deployment-contract verification, controlled replay/live-path tracing, automated tests, and browser verification. Real EKS/AWS runtime trace remains out-of-band unless the user reopens it.
- Keep this plan document current if implementation discoveries change the plan, especially around AWS runtime wiring, S3 prefixes, API contracts, or data-status semantics.

## Current Pipeline Map

### Live Ingestion

Current entrypoint:

- `systems/market-data/pods/market-ingestor/market_stream.py`
- `systems/market-data/shared/alfaka/alpaca/websocket_collector.py`

Current behavior:

- Connects to Alpaca WebSocket.
- Subscribes seed symbols to configured channels.
- Default universe is S&P 500 through `systems/market-data/config/sp500-universe.json`.
- Frontend first-user Watch List suggestions are S&P 500 large-cap examples such as `AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, BRK.B, JPM, UNH`. These are not the backend source of truth for user watchlists.
- Default configured channels are `bars, updatedBars, dailyBars, statuses`.
- Full-universe collection subscribes to `bars`, `updatedBars`, `dailyBars`, and `statuses`; `trades` are resolved from active chart, watchlist, and hot tier state.
- Raw Alpaca events are written to Kafka raw topics such as `market.raw.bars`, `market.raw.trades`, and `market.raw.daily-bars`.

Important implication:

- Tick/trade collection is tier-driven, not full-universe driven.
- Hot/watchlist trade tier state must be populated in Redis/control-plane state for the ingestor to subscribe beyond active charts.
- Full-universe bar/status collection is now config/data driven; Alpaca subscription caps may still require sharding during AWS validation.
- Docker and k8s images must copy `systems/market-data/config` to the same path used by `ALFAKA_REQUEST_CONFIG`. If the image only copies config to `/app/config`, runtime can silently fall back to a legacy seed instead of the S&P 500 universe.

### Stream Processing

Current entrypoint:

- `systems/market-data/pods/market-processor/local_main.py`
- `systems/market-data/shared/alfaka/streaming/processor.py`
- `systems/market-data/shared/alfaka/streaming/transforms.py`

Current behavior:

- Reads raw Kafka topics.
- Converts trades to ticks, live 1m candles, Redis latest price, interval-specific Redis/WebSocket provisional candles, and volume profile bins.
- Converts `bars`, `updatedBars`, and `dailyBars` to closed candles.
- Builds 5m and 10m closed candles in process memory from closed 1m bars.
- Builds provisional `5m`, `10m`, `1D`, `1W`, and `1M` last candles from recent closed source rows plus current live 1m state, and publishes them through `LIVE_CANDLE_UPDATE`.
- Publishes processed Kafka topics:
  - `market.ticks.v1`
  - `market.candles.live.1m.v1`
  - `market.candles.closed.v1`
  - `market.status.v1`
  - `market.volume-profile-bins.1m.v1`

Important implication:

- 5m / 10m stream aggregation state is in memory.
- Provisional candle derivation state is in memory; Redis-first startup recovery and optional ClickHouse canonical-row recovery are implemented, while Kafka/S3 replay fallbacks remain further hardening before Milestone 5 is fully closed.
- Moving average state is in memory.
- Deduplication state is in memory.
- Process restart, pod rescheduling, or future parallelism can break continuity unless state is rebuilt or checkpointed.

### Storage

Current S3 sink:

- `systems/market-data/pods/s3-sink/processed_sink.py`
- `systems/market-data/shared/alfaka/storage/processed_s3_sink.py`

Current ClickHouse loader:

- `systems/market-data/pods/clickhouse-loader/processed_loader.py`
- `systems/market-data/shared/alfaka/storage/clickhouse_loader.py`

Current behavior:

- S3 sink stores processed Kafka events.
- Raw S3 archive sink stores configured raw Alpaca Kafka topics under `S3_RAW_PREFIX` and emits raw manifest entries under `S3_MANIFEST_PREFIX`.
- Processed and raw S3 sinks support count-based flush, time-based flush, shutdown flush, and retry for data-object and manifest uploads.
- ClickHouse loader stores closed candles, market status, and volume profile bins.
- ClickHouse table `chart_candles` uses `ReplacingMergeTree(inserted_at)` ordered by `(symbol, interval, event_time)`.
- Trade ticks are not loaded into ClickHouse by default.
- Backfill writes raw pages to S3, processed candles to S3, and materializes processed candles directly into ClickHouse.

Important implication:

- Raw live stream data now has a local AWS-contract S3 archive path, but real AWS throughput, object sizing, and retention still need operational validation before broad preload/live reliance.
- Low-volume S3 partitions now have time-based and shutdown flush protection; remaining risks are cost/object-count tuning and runtime observability under real symbol volume.
- Serving queries now use a deterministic latest-row subquery for chart reads, but the approach still needs realistic ClickHouse volume benchmarking before deciding whether to add a materialized serving projection.

### Serving

Current API and provider:

- `systems/api-server/pods/api-server/gops-backend/app/routes/charts.py`
- `systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py`
- `systems/market-data/shared/alfaka/serving/provider.py`
- `systems/market-data/shared/alfaka/serving/clickhouse_provider.py`
- `systems/market-data/shared/alfaka/serving/redis_provider.py`

Current behavior:

- API merges Redis recent candles with ClickHouse historical candles.
- ClickHouse chart reads select the latest row per `(symbol, interval, event_time)` using `inserted_at` and `source_event_id` before direct reads or derived aggregation.
- `5m` and `10m` can be served by query-time aggregation from stored `1m`.
- `1W` and `1M` are served by query-time aggregation from stored `1D`.
- Coverage metadata reports the source interval for derived intervals.
- Chart snapshots now expose `dataStatus` for renderability and `repairStatus` for background repair/preload need.
- Chart snapshots canonicalize candle timestamps, merge Redis live/provisional tail candles without duplicate buckets, and use session-aware renderability so weekend/overnight gaps do not create false intraday GapFill requirements.
- Watch List and Hot Ranking quote summaries use previous regular-session close as the percent-change baseline, while live events update the latest price against the same baseline.
- Quote summaries must not fall back to current-session open when previous-close coverage is missing; missing baselines should surface as unavailable until `1D` GapFill/Backfill repairs the canonical daily source.
- WebSocket delivery filters events by exact symbol and interval.
- `GET /api/charts/hot-symbols` exposes Hot Ranking as a REST snapshot for the frontend.
- Hot Ranking serving order is Redis snapshot first, then one deterministic ClickHouse current-session dollar-volume aggregate query, then a last-resort per-symbol Redis/ClickHouse scan.

Important implication:

- 1W / 1M historical reads remain query-time aggregations, while the processor can now publish Redis/WebSocket provisional last-candle updates when daily/provisional daily state is available.
- 5m / 10m exist in both stream materialization and ClickHouse query-time aggregation, which can create semantic drift.
- Query-time aggregation currently does not explicitly reject partial buckets.
- The per-symbol Hot Ranking fallback is intentionally not the normal path; if it is used often in AWS, the hot-tier publisher or ClickHouse aggregate path is unhealthy.

### Backfill

Current API and worker:

- `systems/api-server/pods/api-server/gops-backend/app/market_data/backfill/service.py`
- `systems/market-data/pods/backfill-worker/main.py`
- `systems/market-data/shared/alfaka/backfill/status.py`
- `systems/market-data/shared/alfaka/backfill/runner.py`

Current behavior:

- API accepts requests for all chart intervals.
- Derived intervals are converted to source intervals:
  - `5m`, `10m` -> `1m`
  - `1W`, `1M` -> `1D`
- Requests are stored in Redis status keys and queued through Redis Streams by default.
- Worker uses Redis Streams consumer-group claim/ack/reclaim semantics, retry attempts, and a dead-letter stream.
- Request status now keeps job metadata such as `jobType`, `sourcePreference`, `idempotencyKey`, `attempt`, `claimedBy`, `claimedAt`, `heartbeatAt`, `checkpoint`, and `streamId`.
- Runner directly supports `initial_load` and `gapfill` jobs for Alpaca historical `1m` and `1D` bars through the existing raw S3 -> processed S3 -> ClickHouse materialization path.
- Processed candle S3 writes can emit per-object manifest entries under `S3_MANIFEST_PREFIX`.
- Historical raw S3 archive writes can emit raw manifest entries under `S3_MANIFEST_PREFIX`.
- Backfill runner uses `coverage-first` source selection: ClickHouse covered-range skip, S3 manifest lookup, bounded date-partition fallback, then Alpaca for unresolved ranges.
- GapFill can query deduped ClickHouse candle timestamps for bounded ranges and repair coalesced missing source buckets rather than fetching the entire requested range.
- `replay_repair` and `correction_replay` are executable for canonical candle repair from processed S3 manifests or raw S3 candle archives. They intentionally do not call Alpaca, and Kafka replay remains a later explicit replay mode.
- `GET /api/charts/backfill/queue` exposes Redis Streams queue metrics: retained stream length, pending count, undelivered lag, backlog count, oldest pending entry, and dead-letter length.

Important implication:

- Queue claim/ack/reclaim, queue observability, duplicate-safe materialization retry tests, processed and raw S3 manifest lookup, bounded internal GapFill detection, and first S3-based replay/correction execution are now locally implemented.
- Full Milestone 4 local implementation is functionally complete for the Redis Streams/S3/ClickHouse path. Kafka replay remains an explicit optional mode, not a v1 closure requirement unless the user chooses it. A dependency-backed exchange calendar remains optional hardening.
- Status TTL defaults to seven days so AWS debugging evidence lasts beyond a one-day incident window.
- `BACKFILL_DEFAULT_LOOKBACK_HOURS` is configured in compose/k8s, but the default range code currently derives lookback from interval target days rather than reading that env var directly.

### AWS Deployment

Repository k8s base includes:

- Alpaca ingestor
- Python market processor
- S3 sink
- ClickHouse loader
- backfill worker
- API server
- frontend
- order workers

Milestone 0 repository manifest gap:

- `infra/k8s/base/kustomization.yaml` does not include `deployment-local-stream-processor.example.yaml`.
- `infra/k8s/overlays/aws/kustomization.yaml` explicitly says the local stream processor is not included because it is not production Flink.
- No actual Flink or production stream processor deployment is wired into the AWS overlay in this repo.

Milestone 1 target runtime contract:

- `infra/k8s/base/deployment-market-processor.yaml` is the explicit Python processor deployment.
- Base and AWS kustomize output must render `alfaka-market-processor` with image `gops-market-processor`.
- The preferred processor consumer group env is `KAFKA_PROCESSOR_GROUP_ID`; legacy `KAFKA_FLINK_GROUP_ID` may remain as a fallback only.
- The processor must fail fast when required Kafka or Redis runtime values are empty or still contain AWS placeholder values.

Reported current AWS runtime shape:

- The stream processor currently runs as a Python process in AWS, not as a real Flink job.
- Redis, ClickHouse, and Kafka are currently running as pods.
- S3 is an external AWS bucket, not a pod.
- Local runtime is configured to use the same real AWS S3 bucket when `S3_ENDPOINT_URL` and `DOCKER_S3_ENDPOINT_URL` are empty.

If the reported Python stream processor is not actually consuming the raw topics and producing the expected processed topics, AWS has this broken path:

```text
Alpaca -> raw Kafka topics -> [missing or miswired processor] -> processed Kafka / Redis / ClickHouse
```

This is still the first root cause to validate operationally: the question is no longer "is there a Flink job?", but "is the deployed Python processor wired to the exact raw topics, output topics, Redis keys, S3 prefixes, and ClickHouse contract expected by this codebase?"

## Interval Support Matrix

| Interval | Current live source | Current stored source | Current backfill source | Current concern |
| --- | --- | --- | --- | --- |
| tick/trade | Alpaca `trades`, active chart symbols only | Kafka processed, S3 processed/live; ClickHouse disabled by default | No direct historical trade backfill path | Active/hot trade tier must be aligned with full-universe bar collection |
| 1m | Alpaca `bars`; trade-derived live candle | ClickHouse `chart_candles` interval `1m`; Redis recent/live | Alpaca historical `1Min` | AWS processor gap can stop all live 1m serving; corrections need deterministic latest-row query |
| 5m | Provisional from recent/current 1m; closed in-memory aggregation from 1m closed bars | Can be materialized by processor and also query-time aggregated from 1m | API converts to 1m backfill | Startup recovery for current bucket still needs hardening |
| 10m | Provisional from recent/current 1m; closed in-memory aggregation from 1m closed bars | Can be materialized by processor and also query-time aggregated from 1m | API converts to 1m backfill | Same as 5m |
| 1D | Provisional from intraday 1m; closed from Alpaca `dailyBars` | ClickHouse `chart_candles` interval `1D` | Alpaca historical `1Day` | Startup recovery for current day still needs hardening |
| 1W | Provisional from closed `1D` plus current provisional `1D` | Query-time aggregation from 1D | API converts to 1D backfill | Closed weekly materialization is not a v1 canonical source |
| 1M | Provisional from closed `1D` plus current provisional `1D` | Query-time aggregation from 1D | API converts to 1D backfill | Closed monthly materialization is not a v1 canonical source |

## Working Architecture Direction

This is the agreed v1 architecture direction for the Goal-mode implementation.

### Planning Stance

This effort should not only patch the currently broken pipe. It should define where the market-data system owns durable state, realtime state, transformation state, and recovery state so the pipeline can be operated and evolved safely.

### Canonical Sources

- Treat Alpaca `1m` bars and `1D` bars as the canonical historical candle sources.
- Treat Alpaca trades as the canonical tick/trade source and as the source for provisional live candles.
- Official closed `1m` and `1D` bars should reconcile or replace provisional live candles for the same bucket.
- Derived chart intervals should be reproducible from canonical source intervals:
  - `5m`, `10m` from `1m`
  - `1W`, `1M` from `1D`

### Historical Storage

Store canonical historical candles first:

- Persist durable raw data to S3.
- Persist canonical processed `1m` and `1D` candles to ClickHouse.
- Derive historical `5m`, `10m`, `1W`, and `1M` at query time from deterministic, deduped source rows.

Materialized derived candles may be added later as rebuildable projections or caches if query-time aggregation becomes too slow under real workload.

### Range Targets And Cache Limits

Keep these concepts separate:

- Visible response size: how many candles the API returns to the client for an initial view or pagination request.
- Redis hot-cache cap: how many recent closed candles Redis keeps for fast chart opening and realtime merge.
- Historical coverage target: how much canonical history ClickHouse/S3 should hold.
- Backfill chunk size: how large each worker repair/load job should be.

Planning constants:

- Use regular-session stock assumptions for sizing: 390 trading minutes per day and about 252 trading days per year.
- Alpaca historical availability is treated as up to about 6 years. The v1 GOPS preload target is intentionally smaller: `1D` keeps the 3-year daily range, while `1m` is capped to the 2026-06 operating window back through 2025-04 inclusive.
- Canonical historical sources remain Alpaca `1m` bars and `1D` bars.

Historical coverage target:

| Interval | Source interval | v1 target bars per symbol | Storage rule |
| --- | --- | ---: | --- |
| `1m` | `1m` | scoped monthly preload from 2026-06 window through 2025-04 inclusive | Store canonical rows; reject starts before `2025-04-01T00:00:00Z` |
| `5m` | `1m` | derived from stored scoped `1m` rows | Derive from canonical `1m` |
| `10m` | `1m` | derived from stored scoped `1m` rows | Derive from canonical `1m` |
| `1D` | `1D` | about 756 | Store canonical rows |
| `1W` | `1D` | about 156 derived bars | Derive from canonical `1D` |
| `1M` | `1D` | about 36 derived bars | Derive from canonical `1D` |

Redis hot-cache target:

| Interval | Recommended closed-candle cap | Rationale |
| --- | ---: | --- |
| `1m` | 780 | Two regular trading days |
| `5m` | 156 | Same two-trading-day window derived from `1m` |
| `10m` | 78 | Same two-trading-day window derived from `1m` |
| `1D` | 756 | Full 3-year daily view is small enough to cache if memory allows |
| `1W` | 156 | Full 3-year weekly view |
| `1M` | 36 | Full 3-year monthly view |

Milestone 3 local implementation status:

- Historical target calculations now use the v1 target contract: `1D` keeps 3-year daily coverage, while `1m` preload is bounded by the April 2025 cutoff and should not re-expand to the old 3-year intraday plan without an explicit decision.
- New code should use `historical_target_bars`; legacy `candle_count_for_1y` remains as a compatibility alias only.
- Redis recent closed candle series now trim to explicit interval caps and keep TTL as a safety net, not the primary retention control.
- ClickHouse chart serving now uses a deterministic latest-row subquery for direct reads, derived interval aggregation, coverage, and Hot Ranking fallback aggregation.
- API/chart snapshots include `repairStatus` alongside `dataStatus`; queue-backed GapFill execution remains Milestone 4 work.
- Local Milestone 3 API/browser smoke passed against the latest local API/frontend on separate ports. Remaining gates are realistic ClickHouse performance validation at larger row counts and AWS runtime validation if the user reopens that gate.
- API visible defaults should remain separate from Redis and historical targets; do not return three years of 1m candles by default.

### Historical S3 Prefetch And Replay

Local historical backfill can fetch Alpaca historical data and write it into the shared AWS S3 bucket before production AWS workloads need the same range. This is acceptable because local runtime is configured to use the real S3 bucket when local S3 endpoint values are empty.

Planning direction:

- Use S3 as the durable historical raw/cache layer for Alpaca `1m` and `1D` data.
- Use local execution to preload the initial S&P 500 canonical history into S3 only after Goal-mode implementation, automated tests, local runtime checks, and browser-based verification have passed.
- Preload order is fixed for v1: prove a targeted S3 -> ClickHouse materialization smoke first, preload all 3-year `1D` history second, then preload scoped `1m` history in reviewed dry-run/enqueue windows.
- The operational default for Initial Load is `1D`; `1m` preload must be requested explicitly with `INITIAL_LOAD_INTERVALS=1m` and a reviewed range.
- `1m` preload is bounded by `BACKFILL_INITIAL_LOAD_1M_MIN_START=2025-04-01T00:00:00Z`. April 2025 is included; March 2025 or older `1m` initial-load ranges are forbidden and should be reported rather than deleted if already present.
- Use `S3_PROCESSED_FORMAT=parquet`, `S3_HISTORICAL_RAW_PARTITION_MODE=chunk`, and `S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT=compact` for broad preload.
- Use explicit `S3_MATERIALIZE_KEYS` for smoke materialization; do not use prefix-wide materialization as the first proof.
- Initial Load is an S3 bootstrap job. Unlike GapFill, it must not treat ClickHouse coverage alone as completion because that can leave S3 history missing.
- Initial Load may encounter valid current symbols with no bars in older chunks. These chunks should finish as `alpaca-empty` with an S3 empty marker instead of failing the whole preload.
- Treat the local S3 preload as a final operational/bootstrap step, not as a prerequisite for local tests.
- Let AWS backfill workers check indexed S3 coverage before calling Alpaca again.
- Do not make latency-sensitive chart requests list or scan S3 objects directly.
- Treat local-created S3 data as replay input only after validating object schema, prefix, symbol, interval, range, and checksum/row count metadata.
- Keep the S3 object layout compact enough to avoid excessive small-object PUT/GET/LIST costs.
- Historical raw S3 object names must include a range/job-derived suffix so overlapping local preload windows do not overwrite each other. Historical preload uses chunk-level raw objects rather than one raw object per trading day.
- Do not use local prefetch as a substitute for AWS GapFill logic. It reduces repeated Alpaca fetches, but production still needs gap detection, repair, and idempotent materialization.
- Broad `1m` preload should run in bounded monthly or smaller windows with Redis Streams worker parallelism limited by observed Alpaca/S3/ClickHouse behavior. The locally proven pattern is: enqueue only missing chunks, run a small worker pool, wait for queue backlog to return to zero, verify S3 evidence and ClickHouse materialization, then start the next window. Do not continue the backward loop past the `2025-04` window.

Cost stance:

- This should usually be favorable for repeated backfills, replays, and disaster recovery because it avoids repeated Alpaca historical API calls and can reduce AWS worker time.
- It is not free. S3 storage, PUT, GET, LIST, lifecycle, and possible transfer-out charges still apply.
- Trades should not be prefetched or archived full-universe in v1 unless a later use case proves the value.

### State Ownership

- S3 owns durable raw evidence and replay input.
- ClickHouse owns deterministic historical serving state for canonical `1m` and `1D` candles.
- Derived historical intervals are read models built from deduped canonical rows, not separate sources of truth.
- Redis owns ephemeral realtime state: latest price, live/provisional candles, active symbols, pub/sub events, and short-lived caches.
- The Python stream processor may hold in-memory transformation state only when that state can be rebuilt from Kafka, Redis, ClickHouse, or S3 after restart.
- Backfill state must move toward durable claim/ack/retry semantics rather than a fire-and-forget list.

Kafka should be treated as a recent replay log, not as the only owner of processor state. Kafka can recover recent unprocessed events if offsets, retention, and commits are configured correctly, but it does not by itself reconstruct the latest 1D/1W/1M provisional state or prove historical coverage after retention expires.

Processor recovery priority:

1. Restore fresh live/provisional state from Redis if present and not stale.
2. Rebuild current-day/week/month state from deterministic ClickHouse canonical rows.
3. Replay recent Kafka raw events when offsets and retention cover the outage window.
4. Replay S3 raw/archive data for longer repairs or historical reprocessing.

The Goal-mode implementation should include fallback paths for each layer rather than assuming one mechanism always works.

### Realtime Last-Candle Behavior

Every supported chart interval should have an updating last candle:

- `1m`: trade-derived provisional candle, then official closed `1m` bar.
- `5m`, `10m`: provisional current bucket derived from live/current `1m` state, then closed derived candle.
- `1D`: provisional current day from intraday state, then official daily bar.
- `1W`, `1M`: provisional current week/month from daily/intraday state, then closed derived candle.

The UI should be able to distinguish provisional and closed candles through a stable contract such as `isClosed=false` for the current unfinished bucket.

Provisional candles are not canonical persisted candles in v1. Treat them first as Redis/live/WebSocket state. The initial event strategy is to extend `LIVE_CANDLE_UPDATE` to every chart interval and include fields such as `isClosed=false`, `sourceInterval`, and `updatedAt`.

### Provisional Candle Event Contract

Use `LIVE_CANDLE_UPDATE` for every interval's in-progress last candle rather than introducing a separate event type in v1.

Envelope fields:

- `type`: `LIVE_CANDLE_UPDATE`
- `symbol`: chart symbol such as `NVDA`
- `interval`: target chart interval, one of `1m`, `5m`, `10m`, `1D`, `1W`, `1M`
- `sourceInterval`: source interval used to build the provisional candle, such as `trades`, `1m`, or `1D`
- `source`: `alpaca.trades`, `derived.live`, or another stable source label
- `feed`: Alpaca feed such as `sip`
- `cursor`: cursor for the target interval bucket
- `eventId`: id derived from event type, symbol, interval, and cursor

`data` fields:

- `timestamp`: target interval bucket start time
- `open`, `high`, `low`, `close`, `volume`: current OHLCV state
- `isClosed`: `false` for provisional updates
- `sourceInterval`: repeat the source interval for frontend merge logic
- `updatedAt`: time the provisional candle was last updated

Interval source rules:

- `1m`: live candle from trades, replaced by official Alpaca `bars` close.
- `5m`, `10m`: current bucket from current live `1m` plus recent closed `1m`.
- `1D`: current day from today's closed `1m` plus current live `1m`, replaced by official Alpaca `dailyBars` close.
- `1W`, `1M`: current week/month from closed `dailyBars` plus today's provisional `1D`.

Merge rules:

- `LIVE_CANDLE_UPDATE` updates the current `(symbol, interval, timestamp)` bucket.
- `CANDLE_CLOSED` replaces the matching provisional bucket with a closed candle.
- `CANDLE_CORRECTED` replaces the matching closed candle.
- Provisional rows should not be inserted into canonical ClickHouse candle storage in v1.

Redis key direction:

- Generalize live candle keys from `candle:{symbol}:1m:live` to `candle:{symbol}:{interval}:live`.
- Keep recent closed candle series separate from live/provisional keys.

### Tick/Trade Collection Policy

Backfill and long-term chart continuity require the full universe to accumulate canonical bar data. Do not jump directly to collecting every trade for every possible symbol. Use a split policy:

- Full universe target: S&P 500 symbols. Collect `bars`, `updatedBars`, `dailyBars`, and `statuses`.
- Active charts: collect trades for symbols currently visible to users.
- Hot/watchlist tier: optionally promote frequently viewed or explicitly watched symbols to continuous trade collection.

This keeps costs, Alpaca subscription limits, Kafka load, Redis memory, and S3 volume controlled while still allowing the system to expand toward broader trade capture as usage proves the need.

Trades are primarily for current price, the in-progress last candle, tick charts, and volume profile. They should not be treated as the required backfill source for historical candles while Alpaca `1m` and `1D` bars remain available.

Universe and trade-tier membership must be data/config driven, not hardcoded into backend logic. The S&P 500 universe should come from a symbol registry or generated config with a clear refresh/rebalance process. Active, hot, and watchlist trade tiers should be computed from runtime signals such as open chart sessions, user watchlists, and ranking rules.

### Hot Tier And Hot Panel Direction

Use a dynamic hot tier as one input to trade subscription control.

Decision for v1:

- Hot ranking is S&P 500 top 20 by current-session dollar volume.
- Keep the size config-driven, for example `HOT_TIER_SIZE=20`, not hardcoded into backend logic.
- Compute dollar volume from full-universe `1m` bars: prefer `volume * vwap` when VWAP is available, otherwise use `volume * close`.
- During premarket/market open before enough current-session `1m` data exists, fall back to the latest `1D` bar or recent daily dollar-volume baseline.
- Recompute after each full-universe `1m` bar cycle, but publish and apply subscription changes on a calmer cadence such as every 1 to 5 minutes.
- Use hysteresis to prevent subscription churn:
  - promote a symbol only after it qualifies for the hot set for consecutive evaluations;
  - keep an existing hot symbol until it falls below a wider threshold, such as rank 30, or stays out for a cooldown window;
  - never remove a symbol from trade subscription if it is still required by active chart or watchlist tiers.
- Resolve the final trade subscription plan as a union of active chart, watchlist, and hot symbols.
- If Alpaca subscription caps are hit, use priority order: active chart first, watchlist second, hot tier third.

Senior caveat:

- Pure dollar-volume ranking will be stable and operationally useful, but it will usually favor mega-cap names.
- That is acceptable for the first trade-subscription tier because those symbols are genuinely liquid and likely user-relevant.
- If the UI meaning of "hot" later expands to "unusually active today," that should be a new ranking mode or secondary score, not a change to the v1 top-20 dollar-volume contract.

Hot tier state contract direction:

- Store the resolved hot list in Redis/control-plane state with rank metadata.
- Include at least: `rank`, `symbol`, `name`, `lastPrice`, `changePercent`, `sessionDollarVolume`, `sourceUpdatedAt`, `rankingWindow`, and `rankReason`.
- Publish the same resolved list to the subscription controller and to the frontend-serving path so the UI and ingestor do not compute different hot sets.
- Treat this list as ephemeral realtime state, not canonical historical data.
- The frontend/API fallback path may compute the same top list directly from ClickHouse with one aggregate query when Redis has no hot snapshot; it must not make hundreds of synchronous per-symbol queries during normal operation.

API decision:

- Add a new read endpoint for the frontend hot list: `GET /api/charts/hot-symbols`.
- Do not overload `/api/charts/symbols`; keep that endpoint focused on the symbol/watchlist summary contract.
- The hot endpoint should return a snapshot suitable for first render and polling fallback.
- Recommended response shape:
  - `ranking`: metadata such as `method=current_session_dollar_volume`, `universe=sp500`, `limit=20`, `asOf`, `refreshSeconds`, and `sourceUpdatedAt`.
  - `symbols`: ranked records with `rank`, `symbol`, `name`, `market`, `lastPrice`, `changePercent`, `volume`, `sessionDollarVolume`, and `rankReason`.
- WebSocket updates can be added later for lower-latency panel refresh, but the REST snapshot should be the stable base contract.

Frontend direction:

- Add Hot as a first-class workspace panel type, not only as a system sidebar mode.
- Planned frontend panel type: `hotRanking`, title `Hot Ranking`.
- Register it beside existing panel categories such as `chart`, `newsFeed`, `orderTicket`, and `watchlist`.
- Add it to the panel catalog and default Chart layout so users and future layout agents can place, move, pin, resize, or immediately see it like other panels.
- Reuse the current Watch List row presentation pattern where possible, but keep the semantics separate: Watchlist is user-curated; Hot Ranking is market-derived.
- Show the 20 ranked symbols with current price and percent change; include rank and dollar volume when space allows.
- Selecting a hot symbol should open or update the active chart, which then also promotes the symbol through the active-chart trade tier.

### ClickHouse Latest-Row Direction

Do not rely on background `ReplacingMergeTree` merges alone for serving correctness.

Recommended path:

1. Phase 1: update serving queries so API reads select the latest row per `(symbol, interval, event_time)` before aggregation.
2. Phase 2: introduce a serving view, projection, or materialized latest table if the query rewrite is too slow under realistic load.
3. Keep `FINAL` as an operational/debug option, not the default chart query path, unless benchmarking proves it is acceptable.

This gives deterministic correction behavior first, then leaves room for a faster physical read model once the correctness contract is proven.

### Backfill And GapFill Direction

Terminology:

- Chart serving: user-facing reads for visible chart ranges. This should use Redis and ClickHouse, not direct S3 scans or synchronous Alpaca calls.
- Backfill: a data-maintenance job that ensures canonical storage has the expected source interval data for a requested range.
- GapFill: a focused backfill variant that uses existing stored coverage and only fetches or repairs missing canonical buckets.
- Replay/repair: a storage rebuild path from S3 raw or processed objects into ClickHouse or processed projections.

Backfill must be split into clearer job classes:

- Initial load: fill broad historical ranges for the S&P 500 canonical `1m` and `1D` sources.
- GapFill: detect and repair missing canonical buckets using the trading calendar and source interval rules.
- Replay/repair: rebuild ClickHouse or processed outputs from S3 raw/archive data.
- Correction replay: apply updated bars or corrected source data deterministically.

Data lookup order:

- Chart request path:
  1. Redis hot cache for recent closed and live/provisional candles.
  2. ClickHouse deterministic latest-row serving projection for historical canonical/derived candles.
  3. Coverage metadata and backfill status if ClickHouse is incomplete.
  4. Optional asynchronous backfill/gapfill enqueue; do not block the chart request on S3 scans or Alpaca.
- Backfill worker path:
  1. ClickHouse coverage and/or a coverage manifest to identify existing canonical rows.
  2. S3 manifest or exact partition lookup for already archived raw/processed source data.
  3. Alpaca historical API only for missing ranges that are not available in S3.
  4. Materialize or repair ClickHouse from the selected source.

GapFill definition:

- A Redis cache miss is not automatically a GapFill.
- A ClickHouse historical miss for an expected canonical bucket can become a GapFill candidate.
- GapFill should merge adjacent missing buckets into bounded repair ranges rather than enqueue one job per missing candle.
- GapFill should verify success by rechecking deterministic ClickHouse coverage after the job completes.

Backfill does not have to flow through Kafka in v1:

- For historical repair, the recommended path is worker -> raw S3 archive -> processed S3/materializer -> ClickHouse.
- A separate replay mode can later publish S3 raw events back to Kafka when we want to test the stream processor or regenerate all downstream side effects.
- Kafka replay is useful, but it should not be required for every historical GapFill because it increases operational complexity and latency.

Redis hot-cache proposal:

- Redis should be treated as a hot serving cache, not the boundary between "available" and "needs backfill."
- The data beyond Redis should normally come from ClickHouse. Backfill is only needed when ClickHouse/S3 coverage is missing or corrupt.
- Current code stores recent candles in Redis sorted sets with a 7-day TTL and no explicit max-count trim. This should become explicit per interval.
- Do not use one fixed `720` count for every interval. A count means different time coverage for `1m`, `5m`, `10m`, and higher intervals.
- Use the Range Targets And Cache Limits table as the current cap contract:
  - `1m`: 780 bars, about two regular trading days.
  - `5m`: 156 bars, same two-trading-day window.
  - `10m`: 78 bars, same two-trading-day window.
  - `1D`: 756 bars, about three years.
  - `1W`: 156 bars, about three years.
  - `1M`: 36 bars, about three years.
- Keep live/provisional Redis keys separate from recent closed candle series.

Client/history loading proposal:

- Initial chart load should return the visible range plus a modest buffer, not the full intraday preload target.
- Scrolling or range expansion should use cursor/range pagination, for example 300 to 500 returned bars per request for intraday views.
- If ClickHouse has the requested range, serve it immediately without creating a backfill job.
- If ClickHouse is partially missing, return available candles with coverage metadata and enqueue a bounded GapFill if policy allows.
- Broad Initial Load should run as scheduled/admin jobs, not as repeated user-triggered chart requests.
- The initial S3 preload should be run locally only after tests and browser verification pass: `1D` stays on the 3-year range, and `1m` stays within the April 2025 inclusive cutoff.

### Chart Readiness And Repair Status Proposal

Do not collapse chart renderability and repair need into one field.

Recommended response contract:

- `dataStatus`: whether the client can render the requested chart response.
  - `ready`: requested visible range plus buffer is sufficiently covered after Redis + deterministic ClickHouse merge.
  - `partial`: some renderable candles are available, but the requested range is incomplete or sparse.
  - `empty`: no usable candles are available for the requested range.
  - `error`: serving failed in a way the client should treat as an error.
- `repairStatus`: whether background data work is needed.
  - `none`: no repair needed for the requested range.
  - `gapfill_required`: expected canonical source buckets are missing and no active repair is known.
  - `gapfill_active`: a matching repair job is queued or running.
  - `gapfill_failed`: the last matching repair failed or became unavailable.
  - `history_preload_required`: broader canonical preload target is incomplete outside the requested range, but the requested chart can still render.

Decision inputs:

- `requestedRange`: visible range plus server-side buffer.
- `sourceInterval`: `1m` for `1m/5m/10m`, `1D` for `1D/1W/1M`.
- Trading-calendar expected buckets for the source interval.
- Deterministic ClickHouse latest-row buckets.
- Redis recent closed candles and live/provisional candle.
- Symbol lifecycle metadata, such as IPO/listing date and tradable status.
- Existing backfill/gapfill job status for the same symbol/source interval/range.

Initial decision rules:

- Return `dataStatus=ready` and `repairStatus=none` when the requested range is sufficiently covered and no expected source bucket is missing.
- Return `dataStatus=ready` and `repairStatus=history_preload_required` when the requested range is fine but the broader canonical preload target is not yet complete.
- Return `dataStatus=partial` and `repairStatus=gapfill_required` when the response is renderable but expected source buckets inside the requested range are missing.
- Return `dataStatus=empty` and `repairStatus=gapfill_required` when no usable candles are available but the range should be repairable from S3 or Alpaca.
- Return `dataStatus=partial` or `empty` with `repairStatus=gapfill_active` when a matching job is already queued/running.
- Do not mark holidays, weekends, pre-listing periods, or intentionally unsupported extended-hours buckets as gaps.

This keeps the UI simple while allowing the backend to repair data in the background without blocking chart rendering.

S&P 500 impact:

- Because live `1m` and `1D` bars should be collected for the full S&P 500 universe, backfill should shift from user-triggered data discovery toward initial seeding, scheduled coverage audits, and repair.
- After initial load completes, normal user charting should rely on Redis for hot data and ClickHouse for history.
- Backfill triggers should mainly be: a new S&P 500 constituent, AWS downtime, processor/loader failure, ClickHouse/S3 mismatch, historical seed gap, or correction replay.
- Coverage audit should run per symbol and source interval (`1m`, `1D`) and produce GapFill jobs only for missing canonical ranges.

Additional Backfill/GapFill considerations:

- Trading calendar accuracy: holidays, early closes, daylight saving time, and regular-hours vs extended-hours policy.
- Alpaca limits: historical API rate limits, subscription limits, request page size, and per-worker concurrency caps.
- Adjustment policy: decide whether historical bars are adjusted or raw, and keep that consistent across live, backfill, and replay.
- Idempotency: duplicate live/backfill inserts must be safe and deterministic under ClickHouse latest-row serving.
- Coverage index: avoid expensive S3 listing by storing coverage/manifest metadata for raw and processed objects.
- Chunking: split large `1m` loads into bounded date ranges so retries are cheap and failures are local.
- Status retention: keep enough job history to debug AWS failures after more than one day.
- Observability: backfill queue depth and pending/dead-letter state are now exposed; still add per-symbol freshness, retry/backpressure views, and GapFill success/failure counts.
- Backpressure: throttle broad initial loads so they do not starve live processing, ClickHouse inserts, or Alpaca limits.

Queue terminology:

- Redis list is the current simple queue. It can lose a popped job if a worker dies.
- Redis Streams adds consumer groups, pending entries, ack, and reclaim semantics while staying on Redis.
- AWS SQS is a managed queue with visibility timeout and dead-letter queue support, but adds an AWS service dependency.

V1 implementation target: design the job contract independently from the queue backend, then implement Redis Streams for minimal platform change. Keep SQS as a documented fallback only if the user later chooses an AWS-managed queue.

Milestone 4 local implementation status:

- Redis Streams is now the default queue backend through `BACKFILL_QUEUE_BACKEND=streams`.
- `RedisBackfillStore` creates stream entries, stores `streamId`, supports consumer-group reads, claim metadata, heartbeat updates, ack, stale reclaim, retry limits, and dead-letter stream writes.
- The backfill worker now reads stream jobs by consumer name, marks claims, acks terminal jobs, and dead-letters exhausted jobs.
- Backfill job records include job-class metadata and idempotency fields; `initial_load` and `gapfill` route through the current Alpaca historical `1m`/`1D` runner.
- Backfill runner now tries source selection in this order for `coverage-first`: ClickHouse covered-range/internal-gap check, processed S3 manifest, bounded exact partition fallback, then Alpaca historical fetch.
- Processed candle writes now create per-object S3 manifest entries, and the backfill runner reads that manifest before bounded exact partition fallback.
- Historical raw candle archive writes now create raw manifest entries, and replay jobs can read raw archive rows through the manifest or bounded partition fallback.
- Backfill runner can query deduped ClickHouse candle timestamps for bounded GapFill ranges and fetch only coalesced missing source buckets from S3/Alpaca.
- `replay_repair` and `correction_replay` now execute from processed S3 objects or raw S3 candle archives and materialize the recovered rows into ClickHouse through the canonical materializer. `sourcePreference=alpaca-only` is rejected for replay jobs.
- Backfill queue metrics are exposed through `RedisBackfillStore.queue_metrics()` and `GET /api/charts/backfill/queue`.
- Materializer retry tests cover the v1 partial-failure contract: if candle rows are inserted but `load_audit` fails, retry may insert the same candle rows again, and deterministic latest-row serving keeps the result safe.
- Initial Load planning can split broad canonical `1m` / `1D` ranges into bounded chunk jobs and stop enqueueing when queue backlog reaches the configured threshold.
- `systems/market-data/jobs/initial-load/main.py` provides a dry-run-first operational entrypoint for broad Initial Load planning and enqueueing.
- GapFill calendar handling now uses a configured market-calendar adapter (`MARKET_CALENDAR_PROVIDER=configured-nyse`) with timezone, open/close, closed-date, and early-close env contracts.
- A first GapFill helper can compute and coalesce expected missing `1m` / `1D` source buckets while skipping weekends, configured holidays, and configured early closes.
- Coverage repair now forwards leading missing coverage ranges from chart coverage metadata into backfill requests instead of always queuing only the broad default range.
- Local API smoke verified that a `5m` chart backfill request queues a source `1m` stream job without running S3/Alpaca work synchronously.
- Local browser smoke verified that chart rendering, interval switching, Watch List, Hot Ranking, and symbol search remain healthy when S&P 500 universe env is explicitly applied.
- Fresh Milestone 4 API/browser smoke verified `GET /api/charts/backfill/queue`, partial chart metadata, chart rendering, `5m` interval switching, Watch List, Hot Ranking, and Hot Ranking row selection updating the active chart.

Milestone 4 remaining work:

- Extend observability beyond the backfill queue into processor/Kafka/S3/ClickHouse freshness and throughput metrics.
- Kafka replay is not required for Milestone 4 closure under the current v1 S3-first plan; revisit only if the user chooses processor-regeneration replay in v1.
- Optional hardening: replace the configured calendar adapter with a dependency-backed exchange calendar if dependency policy allows.

### Raw S3 Archive Scope

Recommended v1 scope:

- Archive full-universe `bars`, `updatedBars`, `dailyBars`, and `statuses` to S3.
- Archive trades only for active/hot/watchlist tiers.
- Do not archive full-universe trades in v1 due volume, cost, and unclear historical value.

The archive should be partitioned by source channel, date, symbol, and schema version so replay and coverage audits can target exact ranges.

## Implementation Decision Details

These details explain how the Goal-mode implementation should interpret the decision snapshot. They also list fallback options to consider only when tests or runtime constraints prove the primary path is not viable.

### 1. Processor State Recovery Contract

Recommendation:

- Treat Kafka as a recent replay log, not the only state recovery mechanism.
- Keep Python processor in-memory state rebuildable, with no unique state that exists only in RAM.
- On startup, recover in this order:
  1. Load fresh Redis live/provisional keys if they exist and are not stale.
  2. Rebuild current session/day/week/month state from deterministic ClickHouse canonical `1m` and `1D` rows.
  3. Replay recent Kafka raw events when committed offsets and retention cover the outage.
  4. Replay S3 raw/archive data for longer repair windows.
- Rebuild `5m` and `10m` provisional state from deduped current-bucket `1m` rows plus the current live `1m`.
- Rebuild `1D`, `1W`, and `1M` provisional state from canonical daily/minute rows plus the current live candle.
- Move critical consumers toward manual offset commit after successful side effects.

Goal-mode alternatives to keep available:

- If Python recovery becomes too complex, define a later migration path to real Flink or another checkpointed stream processor.
- If Redis live state is stale or missing, skip it and rebuild from ClickHouse plus Kafka/S3 replay.
- If Kafka retention is insufficient, rely on S3 replay and mark the outage as a recoverable data repair, not a realtime recovery.

### 2. ClickHouse Latest-Row Query Strategy

Recommendation:

- Phase 1: fix correctness in API serving queries first.
- Introduce a latest-row subquery or CTE that selects one row per `(symbol, interval, event_time)` using a deterministic latest key such as `(inserted_at, source_event_id)`.
- Make direct `1m` / `1D` reads and derived `5m` / `10m` / `1W` / `1M` aggregations read from this deduped source.
- Keep `FINAL` out of the default chart path unless benchmarked under realistic data volume.
- Phase 2: if query-time dedup is too slow, add a materialized serving projection/table that is rebuildable from canonical rows or S3.

Goal-mode alternatives to keep available:

- Query rewrite only: lowest schema risk, best first correctness step.
- Materialized latest table: faster reads, but adds rebuild/consistency work.
- `FINAL`: simplest operator tool, but likely too expensive as the default API query path.

### 3. S&P 500 Universe Registry And Dynamic Trade Tiers

Recommendation:

- Replace semiconductor-specific defaults with an S&P 500 universe registry.
- Keep the registry data/config driven, not hardcoded into backend logic.
- Initial implementation can use a generated repository config file; later implementation can sync from a managed symbol source.
- Store enough metadata to validate subscriptions: symbol, name, exchange, asset class, tradable status, source, effective date, and tags.
- Full-universe subscription should cover S&P 500 `bars`, `updatedBars`, `dailyBars`, and `statuses`.
- Trade subscriptions should be dynamically computed from:
  - active chart sessions
  - user watchlists
  - hot symbols, defined for v1 as S&P 500 top 20 by current-session dollar volume
  - operational caps from Alpaca subscription limits
- Ingestor should consume a resolved subscription plan rather than embedding symbol/tier policy.
- Hot tier updates should use hysteresis/cooldown so minute-by-minute ranking noise does not cause subscription churn.
- The same hot tier result should feed both the trade subscription controller and `GET /api/charts/hot-symbols`.
- The frontend should expose this as a first-class `hotRanking` workspace panel.

Goal-mode alternatives to keep available:

- Static generated S&P 500 config first for low complexity.
- ClickHouse-backed or service-backed symbol registry later if universe updates and user personalization need stronger operations.
- If Alpaca subscription limits block 500-symbol bars in one connection, split subscriptions across configured shards.

### 4. Backfill And GapFill Job Classes

Recommendation:

- Separate job type from queue backend before choosing Redis Streams or SQS.
- Define four job classes:
  - Initial Load: broad historical load for S&P 500 canonical `1m` and `1D`.
  - GapFill: detect missing expected buckets and enqueue minimal repair ranges.
  - Replay/Repair: rebuild ClickHouse or processed outputs from S3 without calling Alpaca.
  - Correction Replay: reprocess updated/corrected source bars deterministically.
- Use coverage/manifest-first source selection for historical jobs: check ClickHouse coverage and indexed S3 availability before calling Alpaca.
- Do not scan S3 from the chart request path.
- Use trading-calendar-aware expected bucket checks, including holidays and early closes.
- Make every job idempotent by `(jobType, symbol, sourceInterval, start, end, sourcePreference)`.
- Keep historical repair on the direct S3/materializer/ClickHouse path in v1; add Kafka replay as an explicit replay mode later.

Proposed job contract fields:

- `jobId`
- `jobType`
- `symbol`
- `requestedInterval`
- `sourceInterval`
- `start`
- `end`
- `sourcePreference`: `coverage-first`, `alpaca-only`, or `s3-only`
- `priority`
- `idempotencyKey`
- `attempt`
- `status`
- `claimedBy`
- `claimedAt`
- `heartbeatAt`
- `checkpoint`
- `result`
- `error`

Goal-mode alternatives to keep available:

- Start with canonical `1m` and `1D` only; derived intervals remain query-time.
- Add derived materialization jobs later only if serving benchmarks require them.
- If a broad Initial Load is expensive, seed daily first, then intraday by priority tiers.
- If Redis memory pressure appears, reduce hot-cache caps before changing canonical ClickHouse retention.
- If chart pagination creates too many small repair jobs, coalesce GapFill ranges and throttle per symbol.

### 5. Redis Streams Queue Choice

V1 implementation target:

- Keep the job contract independent from the queue implementation.
- Use Redis Streams because Redis is already part of the current pod runtime and this minimizes platform change.
- Use Redis Streams consumer groups with claim, ack, pending inspection, reclaim, retry count, and dead-letter stream semantics.
- Add worker heartbeat and stale-claim reclaim for long-running backfill jobs.
- Keep SQS as the AWS-managed alternative if operations prefer managed visibility timeout, DLQ, CloudWatch integration, and IAM-based access.

Fallback rule:

- Stay with Redis Streams unless the user explicitly chooses AWS-managed queue operations.
- Choose SQS only if AWS-managed visibility timeout, DLQ, CloudWatch integration, and IAM-based access become more important than minimizing platform movement.
- In either case, tests must prove worker crash recovery, duplicate delivery idempotency, retry limits, and dead-letter behavior.

## High-Level Problems

### P0: AWS Python Stream Processor Boundary Must Be Verified And Closed

The repository has an ingestor, sinks, loader, and backfill worker in k8s, but the central raw-to-processed processor is not part of the AWS overlay. The live AWS environment is reported to run the Python stream processor, so the key risk is whether that deployed process exactly satisfies the repo's raw-to-processed contract. If it does not, raw Kafka data can accumulate without feeding Redis, processed Kafka, S3 processed data, or ClickHouse.

Planning decision:

- Validate the deployed Python stream processor against the current raw topics, output topics, Redis keys, S3 prefixes, and ClickHouse row contract.
- Make the Python processor an explicit, documented AWS runtime unit if it remains the current production processor.
- Long-term target may still be a checkpointed Flink processor, but only after the Python processor contract is stable and measurable.

### P0: AWS Realtime Data Can Be Completely Absent

The user observed that realtime data did not enter the deployed AWS system. Goal-mode work must treat this as a root-cause investigation, not only as a UI symptom.

Required trace:

1. Alpaca WebSocket connects with the expected account, feed, credentials, and channel subscription.
2. Full-universe S&P 500 `bars`, `updatedBars`, `dailyBars`, and `statuses` reach raw Kafka topics.
3. Tiered `trades` reach raw Kafka topics for active, watchlist, and hot symbols.
4. The Python stream processor consumes the raw topics with the intended consumer group and emits processed Kafka events.
5. Redis receives latest price, live/provisional candles, active-symbol state, hot-tier state, and pub/sub events under the expected namespace.
6. ClickHouse receives closed canonical candles through the loader/materializer.
7. API REST and WebSocket paths read the same Redis/ClickHouse namespace and forward realtime events.
8. The browser chart visibly updates for an active symbol, and the Hot Ranking panel can show a fresh ranked list.

Likely root-cause classes to check:

- Missing or miswired Python processor deployment in AWS.
- Wrong Kafka bootstrap, topic names, consumer group, offset reset, auth, or auto-commit behavior.
- Alpaca credential, feed, subscription-limit, or symbol-universe mismatch.
- Redis namespace, host, auth, TLS, or active-symbol key mismatch.
- ClickHouse loader consuming processed topics but serving queries reading another table/database.
- Frontend/API WebSocket connected but filtering out events by symbol, interval, or payload type.
- Market-hours assumption: no live trades/bars will arrive when the relevant US market feed is closed.

Exit condition:

- A documented one-symbol local AWS-contract trace or controlled replay proves `Alpaca/raw fixture -> raw Kafka contract -> Python processor -> Redis/processed Kafka -> ClickHouse/API -> browser`.
- Because the current user direction excludes EKS checks, do not block this Goal on a live EKS trace. Keep the actual AWS market-hours trace as an out-of-band operational validation item unless the user reopens AWS verification.

### P0: Kafka Client Semantics Are Development-Grade

Current producer/consumer helpers only configure bootstrap servers and JSON serialization.

Risks:

- No explicit MSK TLS/SASL/IAM configuration.
- Consumers use `auto_offset_reset=latest`, which can skip existing data when a new group starts.
- Consumers use auto commit; failed ClickHouse/S3 writes can still be committed.
- No DLQ or retry topic for poison messages.
- No lag/throughput metrics.

Planning decision:

- Define the AWS Kafka auth mode and required env contract.
- Move critical consumers to explicit commit after successful side effects.
- Add retry/DLQ behavior before broad scaling.

### P0: ClickHouse Correction/Dedup Serving Is Not Deterministic Enough

`chart_candles` uses ReplacingMergeTree. Milestone 3 local work added deterministic latest-row serving queries so the API no longer relies only on background merges for chart reads. Remaining risk is performance under realistic ClickHouse volume and AWS data size.

Risks:

- `updatedBars` may not reliably replace original bars in API results.
- Query-time 5m/10m aggregation can double-count duplicates.
- Backfill and live inserts can overlap and produce duplicate timestamps.

Planning decision:

- Keep the deterministic serving query as the Phase 1 correctness path, then add a materialized projection/table only if performance requires it.
- Decide whether corrections should be represented as replacement state only, or also as auditable event history.

### P0: Backfill/GapFill Reliability Is Only Partially Closed

Redis Streams queue durability, queue observability, coverage-aware GapFill, and first S3-based replay execution are locally implemented, but Milestone 4 is not fully closed until the remaining operational reliability work is complete.

Risks:

- Processed and raw S3 manifest-first lookup are implemented for canonical candle repair, but live raw Kafka-to-S3 archive coverage is still a later durability milestone.
- Gap detection is bounded and can use injected closed/early-close dates, but it is not yet backed by an authoritative exchange calendar or listing-date metadata.
- Replay/repair and correction replay can execute from S3 evidence, but Kafka replay is still only a documented future replay mode.
- Broad jobs still need chunking and retry/backpressure controls before large S&P 500 initial loads.
- Queue depth is now exposed, but broader retry/backpressure controls and cross-pipeline freshness metrics are still needed before large S&P 500 initial loads.

Planning decision:

- Stay on Redis Streams for v1 because it now provides consumer groups with pending/ack/reclaim semantics inside the existing platform.
- Keep AWS SQS as a documented fallback if the user later chooses managed AWS queue operations.
- Finish source selection, range slicing, retry policy, and GapFill rules before considering Milestone 4 complete.

### P1: Realtime Semantics And Recovery Are Uneven Across Intervals

The first Milestone 5 slice adds Redis/WebSocket provisional updates for every chart interval, but recovery and closed-period semantics still need hardening.

Issues:

- `5m` and `10m` receive closed events only after enough 1m candles are seen by the processor; Redis-first and optional ClickHouse current-bucket recovery exist, but Kafka/S3 recovery remains a longer-outage hardening path.
- `1D`, `1W`, and `1M` provisional updates exist as Redis/WebSocket state; Redis-first and optional ClickHouse startup recovery exist, but Kafka/S3 recovery remains the open Milestone 5 hardening path.
- Closed weekly/monthly materialization is not a v1 canonical source; historical `1W`/`1M` serving should remain query-time derived from `1D`.

Planning decision:

- Define what "realtime" means for every interval:
  - tick: every trade or sampled/active trade stream
  - 1m: live intra-minute updates plus closed official bars
  - 5m/10m: provisional current bucket plus closed derived candle
  - 1D: provisional current day plus closed daily bar
  - 1W/1M: provisional current week/month plus closed derived candle
- Extend `LIVE_CANDLE_UPDATE` beyond `1m` and use payload fields to distinguish provisional state from closed/canonical candles.

### P1: Trade Collection Needs Split Full-Universe-Plus-Tiered Policy

Current `trades` subscription is active-chart only. This is efficient and likely intentional, but it means "tick collection" does not cover the whole configured universe.

Planning decision:

- Use a split full-universe-plus-tiered policy as the working direction:
  - S&P 500 full-universe `bars`, `updatedBars`, `dailyBars`, and `statuses`
  - active-only trades for visible charts
  - optional continuous trades for hot/watchlist symbols

The choice affects Alpaca subscription limits, Kafka volume, S3 cost, and Redis load.

Backend implementation must avoid hardcoded universe or tier lists. Symbol universe and tier rules should be supplied by config, registry data, or runtime control plane state.

### P1: Raw Live Data Archive Needs Runtime Validation

The backfill path archives raw historical pages to S3. The live path now has a raw Kafka-to-S3 archive sink in the local AWS-deployment-contract implementation, but this path still needs runtime validation under real message volume and explicit retention/cost tuning.

Risks:

- If the raw archive sink is not deployed or misconfigured, replay still depends on Kafka retention.
- Object partitioning and flush settings can create too many small S3 objects if not tuned under S&P 500 volume.
- Raw-vs-processed reconciliation still needs freshness and coverage metrics.

Planning decision:

- Keep the raw Kafka-to-S3 archive sink as the v1 evidence path for configured Alpaca raw topics.
- Validate deployment wiring, object layout, manifest coverage, retention, and cost under local AWS-contract and later AWS market-hours checks if reopened.

### P1: S3 Sink Flush Policy Needs Runtime Tuning

Processed and raw S3 sinks now flush by count, by elapsed time, and on shutdown, with upload retry. AWS defaults still need runtime tuning.

Risks:

- Very small flush intervals can create excessive S3 object counts and PUT/LIST cost.
- Very large flush counts or intervals can still delay evidence availability for low-volume partitions.
- Operators still need freshness metrics to tell whether S3 archive and processed sinks are caught up.

Planning decision:

- Keep time-based flush, shutdown flush, and upload retry as mandatory sink behavior.
- Consider smaller flush counts for status/candle partitions than ticks.

### P1: Derived Interval Storage Strategy Is Split

Current code both materializes 5m/10m in the processor and derives 5m/10m at query time from 1m. Weekly/monthly are query-time only.

Planning decision:

- Treat `1m` and `1D` as canonical source intervals.
- Make derived historical intervals deterministic at query time first.
- Generate realtime/provisional last candles for all chart intervals.
- Add materialized derived projections later only for performance, with rebuild tests.

### P1: Coverage Checks Need Gap Awareness

Coverage currently reports counts and min/max ranges. That catches empty/incomplete ranges but not all operational problems.

Needed:

- Per-interval expected bucket checks.
- Gap detection by trading calendar.
- Partial bucket classification.
- Duplicate timestamp/correction checks.
- Backfill success criteria tied to source interval quality.

### P2: Worker Health And Observability Are Thin

Current worker deployments have few or no probes, no lag metrics, and no structured operational status endpoint.

Needed:

- Kafka consumer lag.
- Last processed event time per topic/channel/symbol.
- Last successful S3 upload per partition.
- ClickHouse insert success/failure counts.
- Backfill queue depth, running age, retry counts.
- Alpaca WebSocket subscription status and reconnect count.
- Redis active symbol count.

### P2: Environment Contracts Need Hardening

AWS config still contains placeholders and optional secrets. This is okay for templates, but the operational checklist needs to make failures obvious.

Needed:

- Required env validation per pod/job.
- Startup failure with clear message when critical placeholders remain.
- Kafka security env contract.
- Redis TLS/auth env contract if managed Redis requires it.
- ClickHouse secret and URL validation.
- IRSA permissions smoke checks for S3 and Secrets Manager.
- Keep local and AWS S3 prefixes aligned, especially `S3_RAW_PREFIX`, so raw archive and replay paths do not diverge across environments.

## Proposed Target Shape

This is the working target unless we decide otherwise.

```text
S&P 500 universe registry / tier controller
  -> full-universe bar/status subscriptions
  -> active/hot/watchlist trade subscriptions

Alpaca WebSocket
  -> Kafka raw topics
  -> raw S3 archive sink
  -> stream processor
       -> processed Kafka topics
       -> Redis realtime cache/pubsub
       -> ClickHouse serving projection
       -> processed S3 sink

Alpaca Historical API
  -> backfill queue
  -> raw S3 archive
  -> canonical processed candles
  -> ClickHouse serving projection
  -> coverage/gap audit

API server
  -> Redis for live/latest
  -> ClickHouse deterministic serving view for history
  -> backfill status and request API
```

Key design principles:

- Raw events are durable before complex transformation.
- Canonical source intervals are clear: trades/ticks, 1m bars, 1D bars.
- Derived intervals are deterministic and rebuildable.
- Serving queries select latest correction deterministically.
- Backfill jobs are recoverable and idempotent.
- Realtime semantics are explicit per interval.
- AWS deployment includes every required runtime unit.
- Universe and trade tiers are config/data driven, not hardcoded.

## Goal-Mode Milestone Plan

Each milestone has a narrow goal, implementation scope, automated test gate, browser or operational verification gate, and exit condition. A failing gate creates a subgoal; the next milestone should not start until that subgoal is resolved.

### Milestone 0: Baseline Inventory And Guardrails

Goal:

- Establish the exact current runtime, config, and failure baseline before changing behavior.

Implementation scope:

- Read `docs/PRODUCT_CONTEXT.md`, `docs/STRUCTURE_GUIDE.md`, `docs/ARCHITECTURE.md`, `docs/IMAGE_STRATEGY.md`, `docs/ENVIRONMENT.md`, `AGENTS.md`, and the current market-data/API/frontend code touched by this plan.
- Record current Kafka topics, Redis namespace, ClickHouse database/table names, S3 bucket/prefixes, and k8s/compose runtime units.
- Confirm local S3 endpoint behavior and avoid broad S3 writes during tests.
- Confirm Python processor is the near-term stream processor target.

Automated tests/checks:

- Run the narrow existing test suites that cover market-data config, serving, realtime boundaries, and API chart routes.
- Add no broad refactor before this baseline passes or fails with understood reasons.

Browser/ops verification:

- Start the local app if possible and capture the current chart/watchlist behavior before changes.
- Under the current no-EKS direction, capture the one-symbol realtime trace failure point through local AWS-contract checks and controlled replay rather than direct cluster inspection.

Exit condition:

- The Goal has a written baseline: what currently passes, what fails, and where the AWS realtime data path is believed to break.

### Milestone 1: Close The Live Data Path

Goal:

- Fix the root problem where AWS realtime data can fail to enter the system at all.

Implementation scope:

- Make the Python stream processor an explicit documented runtime unit for local and AWS.
- Validate required env vars and fail fast when Kafka, Redis, ClickHouse, S3, Alpaca, or namespace settings are missing or inconsistent.
- Ensure raw Alpaca events flow to raw Kafka topics, then through the Python processor to processed Kafka, Redis, and ClickHouse loaders.
- Move critical consumers toward commit-after-side-effect semantics where needed to avoid silent data loss.
- Add a one-symbol trace tool or runbook for `Alpaca -> raw Kafka -> processor -> Redis/ClickHouse -> API`.

Automated tests/checks:

- Unit tests for topic names, env validation, Redis keys, and processor input/output contracts.
- Local smoke test proving a raw bar/trade fixture or controlled replay creates the expected processed event and Redis state.
- Kafka consumer lag check must be observable and bounded in the smoke environment.

Browser/ops verification:

- Use the browser against the running local app and verify an active symbol can show fresh chart/quote state after the live path is running.
- Under the current no-EKS direction, complete a controlled replay/live-path local test and validate the runtime contract without direct cluster inspection.
- Keep the real AWS market-hours one-symbol trace as a runbook/checklist item, not as this Goal's closure gate, unless AWS verification is reopened.

Exit condition:

- Realtime absence is explained and fixed at the runtime boundary, not merely hidden in the frontend.

### Milestone 2: S&P 500 Universe, Dynamic Trade Tiers, And Hot Ranking

Goal:

- Replace semiconductor defaults with the S&P 500 universe and add deterministic trade-tier control.

Implementation scope:

- Add or generate an S&P 500 registry/config path with symbol, name, exchange, tradable status, source, and effective date metadata.
- Subscribe full universe to `bars`, `updatedBars`, `dailyBars`, and `statuses`.
- Resolve trade subscriptions as active chart + watchlist + hot tier, with priority order active chart > watchlist > hot.
- Implement hot tier as top 20 S&P 500 symbols by current-session dollar volume.
- Default hot cadence unless implementation evidence changes it: recompute after `1m` bar updates, publish every 60 seconds, promote after 2 consecutive qualifying evaluations, demote after rank worse than 30 or 10 minutes outside the hot set.
- Add `GET /api/charts/hot-symbols`.
- Add frontend `hotRanking` as a first-class workspace panel in the panel registry, catalog, and default Chart layout.

Automated tests/checks:

- Registry parsing/validation tests.
- Subscription-plan tests for active, watchlist, hot, cap priority, and no hardcoded semiconductor defaults.
- Hot ranking tests for `volume * vwap`, `volume * close` fallback, premarket/daily fallback, rank metadata, and hysteresis.
- Hot Ranking API/service tests should prove Redis snapshot is preferred and ClickHouse aggregate is used before any per-symbol fallback scan.
- API route tests for `GET /api/charts/hot-symbols`.
- Frontend tests or type checks for `hotRanking` panel registration.

Browser/ops verification:

- Use the browser to verify the default Chart layout shows the Hot Ranking panel and the panel catalog still registers it.
- Verify the panel shows ranked symbols, price/change fields, and selecting a row opens or updates the active chart.
- Verify the existing Watch List still works and is not semantically merged with Hot Ranking.

Exit condition:

- S&P 500 bars/statuses and tiered trades are config/data driven, and Hot Ranking is visible as an independent panel.

### Milestone 3: Deterministic Historical Serving And Readiness

Goal:

- Make historical reads correct under duplicate live/backfill/correction rows.

Implementation scope:

- Implement deterministic latest-row ClickHouse serving for `(symbol, interval, event_time)`.
- Make `5m`, `10m`, `1W`, and `1M` historical aggregation read from deduped source intervals.
- Update target helpers from previous 1-year/5-year assumptions to the v1 preload plan: 3-year `1D`, scoped `1m`.
- Add Redis max-count trimming by interval while preserving live/provisional keys.
- Add `dataStatus` and `repairStatus` response semantics where chart serving detects partial or missing canonical coverage.

Automated tests/checks:

- Original plus updated bar returns the updated value.
- Backfill/live duplicate rows do not double-count.
- Derived intervals handle missing source buckets and partial buckets explicitly.
- Target/preload calculations match the v1 plan and do not imply a 3-year `1m` load.
- Redis recent-series trim tests match the interval caps.
- `ready`, `partial`, `empty`, `gapfill_required`, `gapfill_active`, and `history_preload_required` status tests.

Browser/ops verification:

- Use the browser to switch chart intervals across `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`.
- Verify charts render without duplicate spikes, broken partial buckets, or stale last-candle confusion.
- Verify user-facing loading/partial states do not block a renderable chart.

Exit condition:

- Chart reads are deterministic and can tell the difference between renderable data and repair-needed data.

### Milestone 4: Backfill And GapFill Reliability

Goal:

- Make historical repair recoverable, idempotent, and coverage-aware.

Current local status:

- Redis Streams queue reliability work is implemented and locally verified.
- Processed/raw S3 manifest-first worker lookup, bounded internal ClickHouse timestamp GapFill, and S3-based replay/correction execution are implemented and locally verified.
- Backfill queue observability and duplicate-safe materialization retry tests are implemented and locally verified.
- Initial Load chunk/backpressure planning is implemented and locally verified.
- Configured market-calendar adapter handling is implemented and locally verified.
- Initial Load dry-run/enqueue job entrypoint is implemented and locally verified.
- Fresh API/browser smoke proves partial-history chart data remains renderable, `GET /api/charts/backfill/queue` returns metrics, interval switching works, and Hot Ranking row selection updates the active chart.
- Milestone 4 local implementation is closed for the S3-first v1 plan. External exchange-calendar integration, Kafka replay, and broader processor/Kafka/S3/ClickHouse observability remain optional/future hardening.

Implementation scope:

- Keep Redis Streams consumer groups as the default backfill queue backend.
- Complete job classes: Initial Load, GapFill, Replay/Repair, Correction Replay.
- Continue improving coverage/manifest-first lookup: ClickHouse coverage and internal bucket checks, then indexed S3 availability, then Alpaca only for missing ranges.
- Add idempotency key, attempt count, claim/ack/reclaim, heartbeat, retry, and dead-letter behavior.
- Add trading-calendar-aware GapFill detection for regular hours, holidays, early closes, weekends, and listing dates.
- Keep replay/correction jobs S3-first in v1 unless Kafka replay is explicitly chosen for processor-regeneration testing.

Automated tests/checks:

- Worker crash after claim can be reclaimed or retried.
- Duplicate delivery is idempotent.
- Partial S3 success plus ClickHouse failure retries safely.
- Derived interval requests map to source intervals correctly.
- GapFill queues bounded missing ranges, not one job per missing candle.
- GapFill helper skips weekends/configured closed dates, honors configured early closes, and coalesces adjacent missing buckets.
- Bounded GapFill jobs query deduped ClickHouse timestamps and fetch only detected missing ranges.
- Replay/repair and correction replay materialize from processed or raw S3 evidence without calling Alpaca.
- Job status remains available long enough for AWS debugging.

Browser/ops verification:

- Local completed gate: API smoke proved `5m` backfill requests queue source `1m` stream jobs; browser smoke proved chart rendering, interval switching, Hot Ranking, and symbol search stay healthy with S&P 500 config.
- Local completed gate: API/backfill smoke still queues source-interval stream jobs after manifest changes, and browser smoke still verifies chart rendering, interval switching, Watch List, Hot Ranking, and symbol search.
- Local completed gate: unit tests verify raw manifest lookup and S3-based `replay_repair` / `correction_replay` materialization paths.
- Local completed gate: fresh API/frontend smoke verified queue metrics, partial chart metadata, `5m` interval switching, Watch List, Hot Ranking, and Hot row selection changing the active chart.

Exit condition:

- Backfill failure no longer loses work silently, and GapFill repairs only missing canonical source buckets.

### Milestone 5: Realtime And Provisional Candles For All Intervals

Goal:

- Make every chart interval show an updating last candle with a stable provisional/closed contract.

Current local status:

- Interval-aware live Redis keys are implemented as `candle:{symbol}:{interval}:live` with `1m` preserved as the backward-compatible default.
- The Python processor now emits `LIVE_CANDLE_UPDATE` for `1m`, `5m`, `10m`, `1D`, `1W`, and `1M` from explicit trade/bar fixtures.
- Provisional candles remain Redis/WebSocket state only; they are not inserted into canonical ClickHouse candle storage.
- `sourceInterval` and `updatedAt` are preserved in the WebSocket event envelope/data path and through the chart-engine normalizer.
- API WebSocket Redis fallback now polls the live key for each subscribed interval instead of only `1m`.
- Local automated checks passed for market-data hardening/realtime boundary/API query tests, chart runtime tests, frontend build, Python compile, and diff whitespace.
- Local browser smoke passed on API `127.0.0.1:8013` and frontend `127.0.0.1:5177`: app rendered, Hot Ranking rendered, chart interval switched to `5m`, a controlled Redis `LIVE_CANDLE_UPDATE` reached the WebSocket client, and the chart stream status became `Live`.
- Redis-first processor startup recovery is implemented and tested: recent Redis closed `1m`/`1D` series and live `1m` keys seed the in-memory provisional/live builders before the next trade.
- Optional ClickHouse startup recovery is implemented and tested behind `PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED`; it can rebuild missing startup state from deterministic canonical `1m`/`1D` rows when ClickHouse is known healthy.
- Remaining Milestone 5 subgoal before full closure: keep Kafka/S3 replay as explicit fallback/runbook paths for longer outages and processor-regeneration cases.

Implementation scope:

- Extend `LIVE_CANDLE_UPDATE` to `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`.
- Generalize Redis live candle keys to `candle:{symbol}:{interval}:live`.
- Rebuild processor in-memory state from Redis, ClickHouse, Kafka, or S3 according to the recovery priority. Redis-first and optional ClickHouse recovery are implemented; Kafka/S3 recovery remains the open hardening path.
- Ensure official `bars` and `dailyBars` replace matching provisional buckets.
- Keep provisional candles out of canonical ClickHouse storage in v1.

Automated tests/checks:

- Trade-derived `1m` live updates are replaced by official closed `1m` bars.
- `5m` and `10m` provisional buckets are deterministic from current `1m` state.
- `1D`, `1W`, and `1M` provisional buckets derive from intraday/daily state correctly.
- Processor restart recovery rebuilds current buckets without unique RAM-only state.
- WebSocket event merge tests distinguish `isClosed=false` from closed/corrected candles.

Browser/ops verification:

- Use the browser to observe an active symbol on each supported interval.
- During local verification, use controlled replay of real raw events to verify last-candle updates. Do not claim live EKS/AWS closure unless AWS verification is reopened and a market-hours smoke passes.

Exit condition:

- The UI can render and update the final unfinished candle for every supported interval.

### Milestone 6: S3 Durability, Replay, And Historical Bootstrap

Goal:

- Make S3 a durable evidence and replay layer without creating cost surprises.

Current local status:

- Raw live Kafka-to-S3 archive sink is implemented at `systems/market-data/shared/alfaka/storage/raw_s3_archive_sink.py` with the pod wrapper `systems/market-data/pods/s3-sink/raw_archive_sink.py`.
- Docker Compose and k8s base now include a `raw-s3-archive` runtime unit using the `gops-market-storage` image and the shared market-data config.
- Processed and raw S3 sinks support count-based flush, time-based flush, shutdown flush, and retry for data-object and manifest uploads.
- Raw archive rows normalize Alpaca raw channels such as `updatedBars` to stable partition names such as `updated-bars`.
- Raw and processed S3 manifest entries are emitted under `S3_MANIFEST_PREFIX` so backfill/replay lookup can avoid broad S3 scans.
- Initial Load dry-run now resolves `INITIAL_LOAD_SYMBOLS=universe` from the configured S&P 500 registry and reports chunk count, row/object/manifest estimates, resume strategy, and required S3 validation checks before enqueueing.
- Initial Load resume now skips existing queued/running/succeeded chunk requests without consuming enqueue capacity, so repeated reviewed runs can keep advancing through the S&P 500 chunk plan.
- Historical raw S3 archive object names now include a range/job suffix to prevent same-symbol/same-day overwrites across overlapping preload windows.
- Historical raw backfill defaults to chunk-level raw objects with compact raw/processed manifest entries, so broad preload can stay replayable without exploding small S3 object counts.
- Empty historical initial-load chunks write durable markers under `S3_MANIFEST_PREFIX/empty/candles/...` and count as completed evidence for resume.
- Alpaca historical backfill now sends `HISTORICAL_ADJUSTMENT=raw` by default and retries transient 429/5xx/request failures with bounded backoff.
- S3 materialization can target explicit `S3_MATERIALIZE_KEYS`, which is the required path for the pre-broad-preload S3 -> ClickHouse smoke.
- Local 3-year S&P 500 `1D` dry-run executed for `2023-06-30T00:00:00Z` to `2026-06-30T00:00:00Z`: after removing invalid `FDXF`, the v1 registry has 502 symbols and `1D` has 1,506 planned chunks. The scoped `1m` plan is managed separately by monthly windows and the April 2025 lower-bound guard.
- Local 1D 3-year preload has been executed against the shared AWS S3 bucket: planner evidence check reports 1,506/1,506 completed chunks, with 1,501 processed S3 objects and 5 empty markers. Status row total from completed chunks is 375,039 processed candle rows. ClickHouse `FINAL` smoke over the full 1D range returned 376,559 rows, 505 distinct symbols, and range `2023-06-30 04:00:00.000` to `2026-06-29 04:00:00.000`; the distinct symbol count includes older local/test/pre-fix materialized rows beyond the corrected 502-symbol plan.
- Updated `1m` dry-run for `2026-05-01T00:00:00Z` to `2026-06-01T00:00:00Z` with the 502-symbol registry estimates 3,514 chunks. After the MSFT 1m pilot showed Alpaca historical 1m bars can include extended-hours data, the dry-run estimate now uses `HISTORICAL_1M_MINUTES_PER_TRADING_DAY=960`, producing about 10.6M estimated rows, 3,514 raw chunk objects, 3,514 processed objects, and 7,028 manifest entries for that month.
- Local `1m` preload completed the `2026-05-01T00:00:00Z` to `2026-06-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,012 processed S3 objects, 502 empty markers, and 4,261,039 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,291,862 rows across 504 distinct symbols and range `2026-05-01 08:00:00.000` to `2026-05-29 23:59:00.000`; the distinct symbol count can include earlier/pre-existing local materialized rows outside the exact 502-symbol evidence plan. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2026-04-01T00:00:00Z` to `2026-05-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,012 processed S3 objects, 0 empty markers, and 4,406,557 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,436,444 rows across 504 distinct symbols and range `2026-04-01 08:00:00.000` to `2026-04-30 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3012`, and `remainingCount=0`.
- Local `1m` preload completed the `2026-03-01T00:00:00Z` to `2026-04-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,514 processed S3 objects, 0 empty markers, and 4,598,795 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,627,064 rows across 504 distinct symbols and range `2026-03-02 09:00:00.000` to `2026-03-31 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2026-02-01T00:00:00Z` to `2026-03-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,012 processed S3 objects, 0 empty markers, and 3,986,614 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,004,246 rows across 504 distinct symbols and range `2026-02-02 09:00:00.000` to `2026-02-27 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3012`, and `remainingCount=0`.
- Local `1m` preload completed the `2026-01-01T00:00:00Z` to `2026-02-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,214 processed S3 objects, 300 empty markers, and 4,140,415 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,159,846 rows across 504 distinct symbols and range `2026-01-01 00:00:00.000` to `2026-01-30 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-12-01T00:00:00Z` to `2026-01-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,514 processed S3 objects, 0 empty markers, and 4,318,400 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,336,653 rows across 504 distinct symbols and range `2025-12-01 09:00:00.000` to `2025-12-31 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-11-01T00:00:00Z` to `2025-12-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,012 processed S3 objects, 0 empty markers, and 3,858,990 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 3,877,216 rows across 504 distinct symbols and range `2025-11-03 09:00:00.000` to `2025-11-28 21:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3012`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-10-01T00:00:00Z` to `2025-11-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,507 processed S3 objects, 7 empty markers, and 4,520,835 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,545,736 rows across 503 distinct symbols and range `2025-10-01 08:00:00.000` to `2025-10-31 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-09-01T00:00:00Z` to `2025-10-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,006 processed S3 objects, 6 empty markers, and 4,068,274 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,089,495 rows across 503 distinct symbols and range `2025-09-02 08:00:00.000` to `2025-09-30 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3012`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-08-01T00:00:00Z` to `2025-09-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,005 processed S3 objects, 509 empty markers, and 4,035,123 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,055,605 rows across 503 distinct symbols and range `2025-08-01 08:00:00.000` to `2025-08-29 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-07-01T00:00:00Z` to `2025-08-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,500 processed S3 objects, 14 empty markers, and 4,188,641 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,212,114 rows across 502 distinct symbols and range `2025-07-01 08:00:00.000` to `2025-07-31 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-06-01T00:00:00Z` to `2025-07-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,000 processed S3 objects, 12 empty markers, and 3,854,531 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 3,855,527 rows across 502 distinct symbols and range `2025-06-02 08:00:00.000` to `2025-06-30 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3012`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-05-01T00:00:00Z` to `2025-06-01T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,514/3,514 completed chunks, with 3,000 processed S3 objects, 514 empty markers, and 4,096,549 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,096,549 rows across 500 distinct symbols and range `2025-05-01 08:00:00.000` to `2025-05-30 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3514`, and `remainingCount=0`.
- Local `1m` preload completed the `2025-04-01T00:00:00Z` to `2025-05-01T00:00:00Z` inclusive-cutoff window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,000 processed S3 objects, 12 empty markers, and 4,251,553 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same month returned 4,251,553 rows across 500 distinct symbols and range `2025-04-01 08:00:00.000` to `2025-04-30 23:59:00.000`. A planner resume check returned `createdCount=0`, `skippedExistingCount=3012`, and `remainingCount=0`.
- The `2026-06` bounded catch-up plus `2026-05` through `2025-04` closed-month `1m` preload windows used bounded resume batches plus Redis Streams consumer-group parallelism. The first two windows were stable with three workers; later windows were stable with five workers and a small monthly automation loop that still enforces per-window evidence, planner, and ClickHouse gates before advancing. Worker parallelism reduced elapsed time while preserving one-claim-per-chunk semantics; no failed/unavailable chunks were observed. Do not continue the backward loop into `2025-03`, and do not extend a current-month window into future dates.
- Local `1m` bounded catch-up completed the `2026-06-01T00:00:00Z` to `2026-06-30T00:00:00Z` window against the shared AWS S3 bucket. Evidence check reports 3,012/3,012 completed chunks, with 3,012 processed S3 objects, 0 empty markers, and 4,275,936 processed candle rows in Redis status. ClickHouse `FINAL` smoke over the same bounded June window returned 4,275,936 rows across the 502-symbol registry and range `2026-06-01 08:00:00.000` to `2026-06-29 23:59:00.000`.
- Aggregate Redis evidence for the monthly `1m` preload scope `2025-04-01T00:00:00Z` through bounded `2026-06-30T00:00:00Z` reports 49,196/49,196 completed chunks, 47,320 processed S3 objects, 1,876 empty markers, 62,836,252 processed rows, and zero missing/no-evidence chunks. Exact S3 HEAD checks passed for a sample processed candle object, a sample empty marker, a daily processed object, and a compact manifest entry in the shared AWS bucket.
- ClickHouse materialization smoke for the same monthly `1m` scope reports 62,835,853 `FINAL` rows across the 502-symbol registry, with range `2025-04-01 08:00:00.000` to `2026-06-29 23:59:00.000`. A pre-cutoff query for `interval='1m'` and `event_time < '2025-04-01 00:00:00'` returned zero rows; Redis initial-load status probes for `2025-03` found zero statuses and zero S3 evidence. If older S3 data is later discovered by a targeted manifest audit, report it first and do not delete without explicit operator intent.
- Backend and chart-engine target helpers now use the scoped intraday preload count: `1m=122850`, `5m=24570`, `10m=12285`, while `1D=756`, `1W=156`, and `1M=36` remain 3-year daily-derived targets. The actual initial-load guard is `BACKFILL_INITIAL_LOAD_1M_MIN_START=2025-04-01T00:00:00Z`, and the initial-load entrypoint rejects `2025-03` or earlier `1m` dry-runs/enqueues.
- AAPL `1m` empty-chart investigation found that the API had candle rows but marked recent after-hours sparse bars as `returned_window_sparse`. Renderability now treats sparse gaps as blocking only when both neighboring intraday candles are inside the configured regular session; sparse extended-hours bars can render.
- Local AWS-contract compose services now avoid broad `.env` injection for market-data/backend runtime paths, pin `ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager`, and pin `S3_PROCESSED_FORMAT=parquet` so legacy `.env` values such as `semiconductor-100`, old request config paths, missing `dailyBars/statuses`, or `jsonl` processed output do not override the deployment contract. `/health/config` reports redacted stale-env warning codes.
- Cutoff and chart-stability verification gate: market-data hardening tests, API market-data query tests, frontend chart runtime tests, Python compileall, frontend production build, `git diff --check`, initial-load `2025-03` rejection dry-run, initial-load exact-cutoff `2025-04` acceptance dry-run, Redis/S3 evidence aggregation, S3 HEAD samples, ClickHouse materialization smoke, and browser rendering checks must all pass before this stabilization slice is considered closed.
- Replay smoke is locally covered from live raw archive evidence: a raw archive sink object plus manifest can be found by `replay_repair`, converted into canonical processed candle rows, and materialized into ClickHouse through the canonical materializer path.
- Local automated checks passed for market-data hardening/realtime boundary/API query tests, Python compile, k8s base render, and diff whitespace.
- Remaining Milestone 6 work: real S3 preload only after final tests and browser gates pass with explicit operator intent.

Implementation scope:

- Keep raw live Kafka-to-S3 archive enabled for configured raw topics: full-universe bars/statuses and tiered trades.
- Keep time-based flush, shutdown flush, and upload retry behavior for processed and raw S3 sink data/manifest writes.
- Validate compact object partitioning by channel, date, symbol, and schema version under realistic S&P 500 volume.
- Extend replay/materialization smoke coverage from the current historical raw/processed S3 candle path to live raw archive evidence and, if chosen, Kafka replay.
- Add local S&P 500 preload tooling with dry-run, manifest, checksum/row-count validation, idempotent resume, 3-year `1D` support, and scoped `1m` cutoff enforcement.

Automated tests/checks:

- Low-volume partitions flush within the configured time SLA.
- Raw archived event can be replayed into processed candle state.
- Replay is idempotent under duplicate S3 objects or repeated runs.
- Preload dry run validates ranges, object naming, manifest metadata, and estimated row counts before broad writes.

Browser/ops verification:

- After replay or preload dry run, use the browser to load a chart range that depends on replayed/backfilled history.
- Verify data appears through ClickHouse/API, not by direct S3 reads from the chart request path.

Exit condition:

- S3 can support replay and bootstrap safely; broad real S3 preload is performed only after final tests and browser checks pass, and only with explicit operator intent.

### Milestone 7: Observability, AWS Smoke, And Goal Closure

Goal:

- Prove the stabilized system works end-to-end and is observable enough to operate.

Current local status:

- `scripts/local/check-live-path.py` and `alfaka.tools.live_path_trace` provide a read-only one-symbol trace for API, Redis, Kafka topics, and processor consumer-group lag without using EKS.
- The local trace script defaults to the Docker Compose processor group `alfaka-local-stream-processor` so stale local `.env` legacy `KAFKA_FLINK_GROUP_ID` values do not hide the real committed offset/lag signal. Explicit CLI/env overrides still take precedence.
- Backfill queue metrics are exposed through `GET /api/charts/backfill/queue`.
- API health/config endpoints remain available through `/health` and `/health/config`.
- The Python market processor now writes a lightweight Redis component heartbeat at `pipeline:health:market-processor` with last processed channel, symbol, event time, source event ID, and update time.
- Live path trace reads the processor heartbeat alongside price/live/recent candle Redis evidence, making it easier to distinguish a stalled processor from a frontend-only issue.
- Local automated checks passed for market-data hardening/realtime boundary/API query tests, chart runtime tests, frontend production build, Python compile, k8s base render, and diff whitespace after the heartbeat and trace-script changes.
- Latest local no-EKS realtime smoke used a single IEX Alpaca ingestor connection for `AAPL` after the SIP connection returned Alpaca `406 connection limit exceeded`. This is evidence for an operational root-cause class: account/feed connection caps can make realtime appear absent even when the local pipeline is wired correctly.
- Local AWS-contract trace passed for `AAPL` across `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`: API returned renderable data, Redis had latest price and interval live-candle keys, processor heartbeat was fresh, Kafka topics existed, and the processor consumer group exposed committed offset/lag.
- Local browser verification selected `AAPL` from the Hot Ranking panel and observed chart price/change movement plus `Live` stream status across `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`.
- Final local no-EKS closure gate passed after the trace-script change: automated tests/checks reran successfully, and a final browser smoke confirmed the chart panel itself moved from `281.75/+1.03%` to `281.71/+1.01%` while stream status remained `Live`.
- Follow-up local verification confirmed the current image loads the S&P 500 registry from `systems/market-data/config/market-data-request.json`, `PUT /api/charts/watchlist` persists frontend/user Watch List state to Redis `watchlist:symbols`, `berk` search returns `BRK.B`, and TSLA Watch/Hot percent change displays from previous close rather than the current 1m/session open.
- Follow-up quote verification found partial daily coverage can make only some symbols show previous-close percentages. The serving fix now refuses intraday-open fallback, and a targeted local `1D` GapFill for the default Watch List repaired missing baselines for `AMZN`, `META`, `GOOGL`, `BRK.B`, `JPM`, and `UNH`; all ten default Watch List symbols then matched latest price versus 2026-06-26 close in API and browser checks.
- Final AAPL chart stabilization found three serving/UI edge cases beyond the original after-hours sparse-gap bug: stale Redis recent candles could hide newer ClickHouse coverage, Hot Ranking needed one bounded recent-session ClickHouse aggregate instead of broad historical scans, and S&P 500 search needed registry filtering so old non-universe symbols do not leak into the dropdown.
- Final target-range stabilization keeps old pre-existing ClickHouse/S3 rows untouched, but chart serving now clamps ClickHouse reads to the agreed target floor. In practice `1M` no longer exposes older monthly rows when a large `limit` is requested, and `hasMoreBefore` is computed from the target floor rather than from old storage evidence.
- Final local browser verification on the rebuilt compose stack selected AAPL from Watch List, switched `1m`, `5m`, `10m`, `1D`, `1W`, `1M`, confirmed Watch List and Hot Ranking rows render with previous-close percent changes, confirmed screenshot pixel sampling of the chart area is nonblank, and found no browser console warn/error entries.
- Feed/session stabilization now runs Alpaca ingest as explicit `sip`, `iex`, and `boats` feed profiles. Raw envelopes, stream transforms, Redis latest/live state, ClickHouse candle/status rows, API snapshots, and chart runtime data preserve `feedProfile` and `marketSession`; stored historical rows with missing session metadata are converted at serving time, with daily/weekly/monthly candles falling back to `regular`.
- Drag-left historical fetch now has an end-to-end local contract: the chart requests the older window from the candles API, queues bounded range backfill only when the snapshot is repairable, polls backfill status, and refetches after success. The chart request path remains Redis/ClickHouse-backed and does not synchronously scan S3 or call Alpaca.
- Final AAPL API/browser verification after feed/session changes returned renderable `1m`, `5m`, `10m`, `1D`, `1W`, and `1M` snapshots. Browser verification showed a nonblank chart canvas, visible volume/MA rendering, Watch List and Hot Ranking values, no console errors, and successful drag-left pagination into older candles.
- Final multi-symbol browser drag/backfill verification passed for `AAPL`, `NVDA`, and `TSLA` on `1m`: each chart rendered normally, actual pan/drag moved `rightOffset` away from zero, oldest-range navigation caused the loaded candle count to grow from `390` to `780`, Watch List and Hot Ranking stayed visible, and browser console warn/error logs were empty.
- Final queued backfill smoke passed through `POST /api/charts/backfill` -> Redis Streams -> backfill worker -> status API for an already-covered `AAPL 1m` range. The job completed as `succeeded` with `source=clickhouse`, `sourcePreference=coverage-first`, `skipped=true`, and queue metrics showed `pendingCount=0`, `backlogCount=0`, and dead-letter length `0`.
- Feed access/error health smoke passed without opening new Alpaca connections: a controlled `market-ingestor-boats` component heartbeat with `status=error`, `feedProfile=boats`, supported `overnight/pre/regular/after` sessions, and an Alpaca access/connection error was surfaced by `/health/config` in redacted form.
- Deployment caveat: existing ClickHouse volumes can receive `feed_profile` and `market_session` columns idempotently, but they cannot gain the new feed/session-aware `ORDER BY` without a table rebuild. New deployments use the corrected schema; old volumes should be rebuilt before relying on multiple feed rows for the same symbol/interval/timestamp.
- Final local no-EKS live-path trace for `AAPL`, `NVDA`, and `TSLA` returned `status=ok`, API `dataStatus=ready`, Redis latest/live/recent keys present, Kafka raw/processed topics present, and total raw lag zero. Because the US regular session was closed, this is not a market-hours live-feed proof; the processor heartbeat can expire while no fresh Alpaca events arrive, and real feed arrival still needs a market-hours smoke if AWS/live-market proof is reopened.

Implementation scope:

- Add metrics/logs for Alpaca connection status, subscription plan, Kafka lag, processor throughput, Redis freshness, ClickHouse inserts, S3 uploads, backfill queue depth, retry counts, and GapFill results.
- Add health/readiness behavior for pods where appropriate.
- Update runtime docs, env docs, image/compose/k8s docs, and runbooks touched by the implementation.
- Run the final local AWS-contract realtime no-data root-cause checklist. Keep the direct EKS/AWS trace checklist documented but out-of-band under the current user direction.

Automated tests/checks:

- Run all relevant market-data, API, and frontend tests.
- Run local compose or equivalent smoke tests for ingestor, processor, Redis, ClickHouse, API, and frontend when available.
- Verify env validation fails clearly for missing critical settings.
- Verify metrics/logs expose enough state to diagnose a stalled realtime path.

Final browser verification:

- Launch the app in a browser.
- Verify chart loading and interaction for `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`.
- Verify the active chart receives realtime or controlled replay updates and the last candle updates correctly.
- Verify Watch List still shows current price/change and selecting a symbol updates the active chart.
- Verify Hot Ranking panel can be added, renders top-20 ranked symbols, and selecting a row updates the active chart.
- Verify backfill/partial-history behavior from the UI does not block renderable charts.
- Verify no obvious layout overlap, broken panel registration, or stale loading state appears.

AWS contract verification:

- Locally prove at least one symbol flows through `raw Kafka contract -> Python processor -> Redis/ClickHouse -> API/WebSocket -> browser` using controlled replay or real stored raw events.
- When live Alpaca verification is possible locally, use one active feed connection and record feed/account errors such as Alpaca `406 connection limit exceeded` separately from pipeline wiring failures.
- Do not use EKS for this run. Direct AWS market-hours proof remains an out-of-band validation item unless the user explicitly reopens AWS verification.

Exit condition:

- All automated tests pass, final browser verification passes, local AWS-contract realtime trace passes, direct EKS/AWS verification is documented as out-of-band, and docs reflect the final implementation.

## Goal-Time Risk Register

These are expected issues Codex should actively check while implementing:

- Alpaca limits may prevent a single connection from handling full S&P 500 bars plus tiered trades. If so, shard subscriptions by config rather than hardcoding symbols.
- Market-hours assumptions can make realtime look broken when the feed is closed. Distinguish closed-market silence from pipeline failure.
- Local tests can accidentally write to the real AWS S3 bucket. Use dry-run and explicit confirmation before broad writes.
- ClickHouse latest-row query rewrites may be correct but slow. If realistic benchmarks fail, add a rebuildable materialized serving projection.
- Redis memory can grow if trim caps are missed. Enforce caps by interval and keep TTL as a safety net.
- Redis Streams can still deliver duplicates. Every job and materialization path must be idempotent.
- Backfill can over-enqueue if GapFill does not coalesce ranges. Merge adjacent missing buckets and throttle per symbol.
- Historical adjustment policy must be consistent across live, backfill, and replay before broad preload.
- Timezone, DST, holidays, early closes, and listing dates can create false gaps. Use a trading calendar.
- Frontend panel changes can accidentally degrade existing chart/watchlist/order panels. Browser verification must cover the existing panels too.
- AWS secrets, namespace, auth, and pod wiring can differ from local compose. Env validation and one-symbol trace are mandatory.
