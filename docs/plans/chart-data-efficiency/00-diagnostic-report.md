# 00. Diagnostic Report

Date: 2026-07-10. Scope: source-to-pixel static audit, local test doubles, and code-level counts.
No Alpaca or production data plane was contacted. Two sandbox-blocked credential discovery attempts
from the API tests are recorded below as a hermeticity defect.

## 1. Prior order-flow stabilization audit

| Plan item | Verdict | Evidence and remaining gap |
| --- | --- | --- |
| Intraday bidask 1m/10m/1h, default 10m | Met | The accepted intervals and activation are implemented in `apps/gops-frontend/src/components/ChartPanel.tsx:350-356`; fixed-minute aggregation is covered in `apps/gops-frontend/tests/chartRuntime.test.ts:1468-1570`. |
| Bidask chart uses intraday, not daily, data | Met | `ChartPanel` calls intraday at `apps/gops-frontend/src/components/ChartPanel.tsx:927-976`; a source assertion rejects daily use at `apps/gops-frontend/tests/chartRuntime.test.ts:2960-2964`. The separate `OrderFlowPanel` still reads daily by design (`apps/gops-frontend/src/components/OrderFlowPanel.tsx:275-312`). |
| Recompute only the affected order-flow bucket | Not met as a performance criterion | Every minute replacement copies the whole map (`apps/gops-frontend/src/chart/orderFlow.ts:307-316`), while the render cache is a `WeakMap` keyed by that map (`apps/gops-frontend/src/chart/ChartCanvas.tsx:832-854`). One update therefore invalidates all bucket entries, contrary to `docs/plans/orderflow-bidask-stabilization/01-bidask-intraday-chart.md:61-66`. Output is correct; cache locality is not. |
| Redis minute-blob model and constant trade writes | Met | Current minute uses `SET EX`, closed minutes use `ZADD`, and TTL is set once per symbol in `systems/market-data/shared/alfaka/streaming/processor.py:1035-1092`. Fake-clock command tests are at `systems/market-data/tests/test_orderflow_redis_lean.py:82-123`. |
| Release-3 legacy hash removal | Not met | Provider fallback still calls `HGETALL` in `systems/market-data/shared/alfaka/serving/redis_provider.py:119-154`; the old key builder remains at `systems/market-data/shared/alfaka/common/redis_keys.py:45-46`. This is a planned compatibility tail, not an immediate correctness bug. |
| Quote/trade/health Redis command budgets | Met at code level | Five same-window quotes issue five total commands in `systems/market-data/tests/test_orderflow_redis_lean.py:23-60`; trade, order-flow, and health throttles are checked at lines 62-146. The reproduced legacy baseline is checked at lines 170-183. |
| NVDA live-vs-rollup verification tool | Tool met; production sample pending | Verification tests start at `systems/market-data/tests/test_orderflow_verify.py:11`; the operator entry point is `scripts/local/orderflow_verify.py:20`. The recent 5-10 session production report required by `docs/plans/orderflow-bidask-stabilization/03-aggregation-verification.md:77-93` was not run and is delegated to an operator. |
| Pre/post production Redis baseline | Satisfied under steering interpretation | `docs/plans/orderflow-bidask-stabilization/04-redis-lean-strategy.md:39-54,148-166` records that the pre-baseline was not taken and replaces it with fake Redis counts plus an operator after slot. This is meaningful and remains the model for this plan. |

Implementation awkwardness discovered after the prior plan:

- the main processor's in-memory order-flow quote join assumes trade and quote partitions are
  assigned compatibly, but `create_json_consumer` does not select an assignment strategy
  (`systems/market-data/shared/alfaka/common/kafka_io.py:45-80`);
- a rebalance leaves the quote cache empty until a new quote arrives because cache-only mode is set
  in `infra/k8s/base/app/deployment-market-processor.yaml:23-29`;
- the prior tests prove Redis call counts well, but do not prove byte-identical API results between
  legacy and minute-blob readers.

## 2. Measured baseline

### Local validation

| Check | Result |
| --- | --- |
| Python | root `.venv` is Python 3.12.13 |
| market-data unittest | 310 passed, 6 skipped, 0 failed |
| api-server unittest | 174 passed, 0 failed |
| frontend TypeScript | passed |
| frontend Vite build | passed; 870.42 kB JS chunk warning |
| chart runtime tests | passed |
| `docker compose config -q` | passed |
| `docker compose build` | passed |
| `git diff --check` | passed |

The API suite printed two failed AWS Secrets Manager connection attempts while still passing. This
shows a hermeticity gap in credential setup, not an Alpaca data test. Workstream 05 makes all local
tests fail if an external credential/network provider is touched.

### Code-level counts

| Path | Current count | Basis |
| --- | ---: | --- |
| WebSocket Redis recovery, one symbol/one interval | 20 commands/s while idle | Three candle `GET`s + trade `HGETALL` + quote `GET` per 250 ms (`systems/api-server/pods/api-server/gops-backend/app/market_data/realtime/stream_hub.py:80,181-194`) |
| Live candle Redis write, one emitted interval | at least 5 commands | latest + watermark reads, `SET`, `EXPIRE`, and chart-event `PUBLISH` (`systems/market-data/shared/alfaka/streaming/processor.py:912-944`) |
| Same-window quotes, N=5 | 5 total commands | Existing fake Redis test (`systems/market-data/tests/test_orderflow_redis_lean.py:23-60`) |
| Legacy same-window quotes, N=5 | 50 total commands | Reproduced legacy test and recorded table (`systems/market-data/tests/test_orderflow_redis_lean.py:170-183`; `docs/plans/orderflow-bidask-stabilization/04-redis-lean-strategy.md:148-156`) |
| Processed S3, one 502-symbol 1m wave | 1,004 PUTs | 502-symbol fixture; per-symbol buffers (`systems/market-data/shared/alfaka/storage/processed_s3_sink.py:104-146`) flush one data object and one manifest (`:215-266`) |
| Processed S3 lower bound, repeating each minute | 60,240 PUTs/hour | `502 symbols * 60 waves * 2 PUTs`; K8s flush is 10 seconds (`infra/k8s/base/app/configmap.yaml:176-178`) |
| Raw + processed lower bound for bars only | 120,480 PUTs/hour | Raw also buffers by symbol/day and writes data + manifest (`systems/market-data/shared/alfaka/storage/raw_s3_archive_sink.py:94-174`) |

The S3 estimates exclude updated bars, daily bars, trades, quotes, retries, and other intervals; they
are lower bounds, not production observations.

A 1,200-minute synthetic test-fixture probe of transform objects produced 1,200 live-candle
entries, 1,200 moving-average closes, 385 aggregate-window keys containing 4,800 source rows,
1,200 tick `closed_keys`, and 1,200 legacy VP bins. The growth follows directly from append-without-
prune code at `systems/market-data/shared/alfaka/streaming/transforms.py:164-218,343-398,409-479,582-636`.
`ProvisionalCandleState` is the counterexample: it caps 1m/1D rows at 2,000/400 and prunes them
(`systems/market-data/shared/alfaka/streaming/transforms.py:221-312`).

## 3. Source-to-pixel findings

### Collection and Kafka

- The collector has a bounded async Kafka publish queue
  (`systems/market-data/shared/alfaka/alpaca/websocket_collector.py:192,363-420`). This is sound.
- The configured universe has 502 symbols, default bars/daily/status channels, active-chart
  trades/quotes, and five pinned order-flow symbols
  (`systems/market-data/config/market-data-request.json:2,32-49`).
- Trades and quotes have 12 Kafka partitions; every other topic has 3
  (`infra/k8s/base/platform/kafka-topic-init-job.yaml:40-55`). The main processor consumes all six
  input topics with three replicas (`infra/k8s/base/app/deployment-market-processor.yaml:9,23-29`).
- No code consumer of `market.layer.candles.live.v1` exists. The processor publishes it at
  `systems/market-data/shared/alfaka/streaming/processor.py:925-944`; repository references outside
  the producer are topic initialization, tracing, and monitoring labels.
- `platform/kafka/topics.txt` omits the two derived topics present in
  `infra/k8s/base/platform/kafka/topics.txt:30-31`; the inventories are not a single contract.

### Processing and Redis

- Every accepted trade updates a legacy `VolumeProfileBinBuilder` and writes `ZADD + EXPIRE`
  (`systems/market-data/shared/alfaka/streaming/processor.py:607-620,1027-1032`). The active volume
  profile API instead computes from candles (`systems/market-data/shared/alfaka/serving/provider.py:213-222`).
  This is an unconsumed, market-activity-proportional write.
- `LiveCandleBuilder`, `MovingAverageState`, `CandleAggregator`, two `closed_keys` sets, and the old
  VP builder have no pruning at the cited transform lines. `SourceEventDeduper` is capped at 10,000
  but uses `list.pop(0)` for every subsequent event
  (`systems/market-data/shared/alfaka/streaming/transforms.py:582-598`).
- The new order-flow path is bounded by flush time, not trade count, and is the placement model to
  retain (`systems/market-data/shared/alfaka/streaming/processor.py:1035-1092`;
  `systems/market-data/tests/test_orderflow_redis_lean.py:82-123`).
- The WebSocket hub receives the same live events through pub/sub and then polls all live keys after
  every `get_message` call
  (`systems/api-server/pods/api-server/gops-backend/app/market_data/realtime/stream_hub.py:104-119`).
  Polling is useful for recovery, but 4 Hz is not.
- Redis documentation still names the old order-flow hash at `platform/redis/README.md:38,51`, while
  code writes `live-minute` and `minutes`.

### ClickHouse and S3

- `trade_ticks` and `quote_ticks` have no TTL (`infra/clickhouse/initdb/01-market-data.sql:7-50,329-330`).
  Their active readers are order-flow daily rollup and verification, not chart serving
  (`systems/market-data/shared/alfaka/orderflow/rollup.py:352-387`).
- The local DDL lacks `chart_derived_artifacts`; the K8s DDL creates it with TTL
  (`infra/k8s/base/platform/clickhouse-initdb/01-market-data.sql:104-125`). Runtime auto-schema is
  disabled in K8s (`infra/k8s/base/app/configmap.yaml:193`), so a fresh local/K8s environment can
  expose different tables.
- The same DDL diff also shows local-only `agent_graph_expansions`. This plan will not remove that
  agent table; it requires its owning domain to reconcile it.
- K8s processed S3 subscribes to candles/events only
  (`infra/k8s/base/app/configmap.yaml:175-178`); compose also subscribes to trades/quotes
  (`docker-compose.yml:653-672`). Platform S3 docs claim final trades/quotes
  (`platform/s3/README.md:17-28`). K8s behavior is chosen as canonical because raw tick archive plus
  ClickHouse already own tick evidence.
- S3 Terraform enables versioning and encryption but has no lifecycle rule
  (`infra/aws/terraform/main.tf:39-68`). Raw data and object count therefore have no bound.

### API and frontend

- Candle snapshots correctly prefer Redis, merge ClickHouse, and invoke bounded fill
  (`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:67-121`).
- Background fill singleflight is a process-global set
  (`systems/api-server/pods/api-server/gops-backend/app/market_data/fill/service.py:53-55,423-464`),
  so two API replicas can run the same S3/Alpaca repair.
- Compare calls Alpaca directly for each symbol on a cache miss
  (`systems/api-server/pods/api-server/gops-backend/app/market_data/compare/service.py:48-124`) instead
  of consulting canonical candle storage first.
- Volume profile uses the worker client
  (`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:214-240`), but
  indicators calculate inline and cache only in Redis
  (`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:242-308,957-991`).
  The worker writes Redis plus
  ClickHouse artifacts (`systems/market-data/pods/chart-derived-data-worker/main.py:87-105`). This is
  accidental mixing, not a policy.
- The frontend derived maps have TTLs but no size bound or expired sweep
  (`apps/gops-frontend/src/chart/cdcClient.ts:70-77,272-289`). The profile effect sends `priceMin` and
  `priceMax` (`apps/gops-frontend/src/components/ChartPanel.tsx:869-914`), but its stable key omits
  both (`apps/gops-frontend/src/chart/derivedRequestPolicy.ts:8-23`), allowing a stale profile for a
  changed vertical range.
- Switching bidask among 1m/10m/1h reruns the same interval-independent intraday request because
  `chart.interval` is an effect dependency
  (`apps/gops-frontend/src/components/ChartPanel.tsx:918-976`).
- Chart runtime candle arrays are retained for every visited symbol/timeframe key without eviction
  (`apps/chart-engine/src/runtime.ts:56,119,184-188`).
- The frontend performs a 15-second REST candle refresh while a WebSocket session is active
  (`apps/gops-frontend/src/components/ChartPanel.tsx:613-639`), in addition to pub/sub and Redis
  recovery polling.

## 4. Historical document conflicts

| Existing statement | Current evidence | Decision |
| --- | --- | --- |
| `docs/CHART_DATA_REBUILD_PLAN.md:225-232` assigns optional indicators and candle VP to a Kafka worker plus ClickHouse artifacts. | Indicators already bypass that worker (`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:242-308`), and only the worker/API cache loop reads artifacts (`systems/market-data/shared/alfaka/serving/chart_derived_data.py:268-358`). | Supersede it with request-time shared server calculation and Redis TTL cache. Arbitrary request hashes do not justify durable storage. |
| `platform/redis/README.md:38-51` documents the old order-flow hash. | Current processor writes minute blobs (`systems/market-data/shared/alfaka/streaming/processor.py:1035-1092`). | Update docs now; remove the fallback only through CC-6. |
| `platform/s3/README.md:17-28` says processed final includes trades/quotes. | K8s processed topics contain candles/events (`infra/k8s/base/app/configmap.yaml:175-178`), while compose alone adds ticks (`docker-compose.yml:653-672`). | Treat K8s as canonical; keep ticks in raw S3 + bounded ClickHouse and align compose. |
| `platform/clickhouse/README.md:23-27` points to `03-market-data.sql`. | Both actual market-data init files are named `01-market-data.sql`; their contents currently differ. | Generate/reconcile the two real `01` contracts and fix the README. |
| Prior order-flow plan says affected-bucket-only invalidation and release-3 fallback removal. | Whole-map cache invalidation and legacy fallback remain, as recorded in section 1. | Carry both forward as WS03 and WS07; do not reinterpret them as complete. |
| Prior verification requires recent production NVDA sessions. | Local lacks Alpaca credentials and production access; code-level tool/tests pass. | Do not block implementation. Preserve an operator after-deploy slot, per the steering instruction. |

## 5. Answers to the seven review questions

1. **Are all writes justified by a reader?** No. Legacy tick VP Redis writes/table and the live
   candle Kafka topic have no active product reader. Derived ClickHouse artifacts have only the API
   worker/cache loop as a reader; no independent consumer requires durable request hashes.
2. **How many copies/calculations?** Closed candles have justified hot/query/rebuild copies in
   Redis/ClickHouse/S3. Ticks have Kafka, ClickHouse, and raw S3 copies; bounded retention makes the
   latter two acceptable for rollup/repair. Candle VP is calculated in the provider and worker path,
   while optional indicators use a separate API path. Compose adds an unjustified final tick copy.
3. **What grows without a bound?** Six processor state collections, frontend request/runtime maps,
   ClickHouse ticks, raw S3 bytes, and S3 object count. Redis candle/order-flow state is capped/TTL.
4. **What market-rate work can become constant?** Legacy VP writes can become zero. S3 partitions
   can be shard/time-window proportional instead of symbol proportional. Redis quote/order-flow
   writes are already throttle/flush proportional and should remain so. Tick classification and raw
   ingestion must remain market-rate because their consumers need every trade/quote.
5. **What does the frontend repeat?** It repeats intraday order-flow fetches on interval changes,
   polls REST while WS is healthy, and retains exact-range derived cache entries indefinitely. The
   short cache can hit only an identical key within 5/10 seconds; the profile identity is incomplete.
6. **What could disappear unnoticed; what is missing?** Old VP writes, the live candle Kafka topic,
   and final tick S3 in compose could disappear unnoticed. Missing pieces are a canonical internal
   candle query facade, cross-replica fill singleflight, bounded client caches, and one documented
   storage/key/topic inventory.
7. **Where would a new consumer fail first?** It would encounter conflicting DDL/topic files, stale
   Redis/S3 docs, frontend-only order-flow interval aggregation semantics, and direct provider
   shortcuts. The remedy is documented existing-route contracts and shared calculation modules, not
   a speculative analysis API.

## 6. Unknowns and operator-only measurements

Unknown locally: production event rates, Redis CPU/time by command, ClickHouse bytes/day and merge
load, S3 request distribution, Kafka lag, and whether an out-of-repository live-candle/derived-topic
consumer exists. These do not block code-level work. Before disabling a topic producer, an operator
must inspect consumer groups. After each deploy, the operator fills the measurement section in the
implementation report. No pre-change production baseline will be attempted.
