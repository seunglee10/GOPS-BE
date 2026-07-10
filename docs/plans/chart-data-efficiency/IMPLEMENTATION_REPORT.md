# Chart Data Efficiency Goal Report

## Goal

- Workstreams: WS01, WS02, WS03, WS04, WS06, WS05, WS07
- Commit(s): none; user requested no commit/push
- Started/completed: 2026-07-10 / 2026-07-10
- Status: complete

## Scope Delivered

| Workstream | Result |
| --- | --- |
| WS01 visual baseline | Added fixture-only Playwright at 1440x900 and 390x844, 16 baseline images, nonblank canvas and chrome-overlap assertions. Derived client caches are 64-entry TTL/LRU; chart runtime retains active keys plus 8 inactive keys. |
| WS02 processor state | Bounded live candle, MA, aggregate, closed-marker, and dedupe state; added correction recompute through one canonical window read; removed the tick-derived volume-profile writer/builder. |
| WS03 realtime delivery | Split global Redis pub/sub delivery from five-second recovery, kept one session snapshot, added quote fallback cache, explicit Range assignor, order-flow minute bucket cache, and interval-independent intraday fetch. Removed final live-candle Kafka publication in WS07. |
| WS04 canonical query | Added `CanonicalCandleQuery`; candle, compare, agent chart context, and derived callers now share provider/fill semantics. Added Redis `SET NX EX` owner-token singleflight for fill across API replicas. |
| WS06 S3 layout | Added deterministic `final-v2`/`raw-v2` minute/hour/32-shard objects, canonical sort/dedupe, v1+v2 dual readers, no v2 per-object manifests, and rows/objects/retries/duplicates/LIST metrics. |
| WS05 derived ownership | Indicators and candle volume profile share an API request-time service, canonical candle reads, Redis TTL cache, process/distributed singleflight, and calculation/read counters. Removed queue/artifact paths in WS07. |
| WS07 retention/contracts | Added 21-day tick TTL + operator migration, 30-day raw/raw-v2 Terraform lifecycle, reconciled DDL/topic/processed-topic contracts, removed legacy Redis fallbacks and worker/topic/table fresh-install paths, and added the durable operator runbook. |

No chart axis, price scale, zoom, renderer geometry, order/KIS behavior, agent analysis API,
or public chart route was changed. No production, Alpaca, Secrets Manager, broker deletion,
ClickHouse drop, S3 deletion, commit, or push was performed.

## Decisions Made During Implementation

| Decision | Options considered | Choice and reason | Marker |
| --- | --- | --- | --- |
| WebSocket recovery | 250ms full scans; pub/sub only; pub/sub + bounded recovery | Global pub/sub is steady state; one batched five-second recovery protects against dropped events. | none |
| Candle ownership | Keep route-specific provider shortcuts; canonical facade | One facade prevents compare/derived/agent context from bypassing stored candles and fill coordination. | none |
| Realtime S3 partition | Longer symbol buffers; 32 hash shards; Firehose/table format | CRC32 32-shard minute windows give a fixed object upper bound without a new platform. | CC-4 |
| Optional derived data | Kafka worker; API service; browser-only | API service keeps results available outside the browser while removing queue latency and durable request hashes. | CC-3 |
| Tick retention | Wait for production bytes/day; fixed bound | 21 days covers the verification/repair window without preserving unbounded intermediate data. | CC-5 |
| Final contract cleanup | Keep dormant compatibility code; remove after tests | Removed readers/writers first, then fresh-install definitions and deployment assets; destructive live operations remain operator-owned. | CC-1/2/3/6 |

## Differences From Plan

| Planned | Implemented | Reason | Follow-up |
| --- | --- | --- | --- |
| Remove a supposed 15-second candle REST refresh | Retained the timer | Current code uses it only to refresh the active-symbol TTL; it does not fetch candles. Removing it would break realtime cohort membership. | none |
| DDL byte equality | Normalized equality excludes headers and local-only `agent_graph_expansions` | The plan explicitly forbids taking ownership of the agent table. All market-data table bodies are equal. | agent owner may reconcile its table separately |
| S3 dual mode as migration default | Compose/K8s writer defaults to v2; readers remain dual; direct historical helpers default to v1 | Existing backfill/object tests and historical manifest contracts must remain v1 while realtime writes move to v2. | operator observes prefix metrics |
| Release A/B/C over separate deploys | Repository contains the final Release C state after reader-first/shadow/equivalence tests in earlier workstreams | The approved Goal requested all stages in one repository run. Broker/table/object deletion and consumer confirmation were not collapsed. | operator pre-deploy checks remain |
| Terraform format/validate | Contract tests only | Terraform CLI is not installed locally. HCL was not applied. | CI/operator runs fmt, validate, plan |

No `[CORE-TUNING]` addendum was needed.

## Contract Migration Status

| ID | Repository state | Rollback | Operator action |
| --- | --- | --- | --- |
| CC-1 | Legacy tick VP builder, Redis key/reader, loader/provider, event, and fresh DDL removed. | Redeploy preceding image; no old data was deleted. | none unless dropping an existing old table later |
| CC-2 | Live candle uses Redis + pub/sub/WS only; producer code, env, topic inventory, monitor label removed. | Redeploy preceding producer while broker topic still exists. | confirm no external consumer; broker deletion is separate |
| CC-3 | API inline derived path active; worker pod/deployment/image copy, queue/DLQ, artifact store/table DDL removed. | Redeploy preceding API/worker images; versioned Redis keys do not conflict. | confirm no external topic consumer; old table drop is separate |
| CC-4 | v2 writer and dual reader complete; deterministic replay tests pass. | `S3_REALTIME_LAYOUT_MODE=dual` or preceding v1 writer image. | observe v1/v2 object and request metrics |
| CC-5 | Both init DDLs have 21-day tick TTL; idempotent migration supplied. | extend/remove TTL before expiry. | review backups/parts, then run migration |
| CC-6 | Legacy order-flow hash builder/fallback absent; test proves zero `HGETALL` fallback calls. | preceding image can read existing hash until its natural expiry. | none |

## Code-Level Measurements

No production baseline was collected or attempted.

| Metric/scenario | Before | After | Fixture/spy proof |
| --- | ---: | ---: | --- |
| WS idle Redis, one symbol/interval | 20 commands/s | 5 initial commands, then 5/5s = <=1/s | `test_market_data_realtime.py` |
| Quote path, 5 same-window quotes | 50 commands | 5 commands | `test_orderflow_redis_lean.py` legacy/current pair |
| Trade live state, 5 same-window trades | 10 commands | 2 commands (`HSET` + `EXPIRE`) | `test_trade_live_state_write_is_symbol_throttled` |
| Health, 5 same-window writes | 30 commands | 3 `SET EX` commands | `test_health_write_is_interval_throttled` |
| Order-flow writes | trade-count proportional | 1 live `SET` per flush; 1 `ZADD` per closed minute; session TTL once | `test_order_flow_flush_commands_do_not_scale_with_trade_count` |
| Live candle Kafka send | 1 per emitted live candle | 0 | processor smoke + retired-topic audit |
| Derived request Kafka/artifact write | 1 enqueue, optional DLQ + artifact insert | 0 sends, 0 artifact inserts | `test_chart_derived_service.py` and path removal audit |
| 1,200 sequential-minute state | live 1,200; MA 1,200; aggregate rows 4,800; VP 1,200 | live 2/symbol; MA 60/symbol; aggregate rows 0 after close; VP 0 | diagnostic baseline + bounded-state tests |
| 100,000-minute, 4-symbol state | unbounded by design | live 8; MA 240; aggregate rows 0; dedupe 10,000; closed markers <=2,048 | `test_processor_state_bounds.py` |
| Processed S3, 502-symbol wave | 1,004 PUTs (data + manifest/symbol) | <=32 data PUTs, 0 manifests | `test_processed_s3_sink.py` |
| Processed S3, 502 symbols x 60 waves | 60,240 PUT/hour lower bound | <=1,920 PUT/hour | same shard bound |
| V2 bounded read, one symbol/hour | not bounded by v2 contract | 1 LIST call, 1 requested shard prefix | `test_s3_manifest.py` |
| Derived 10 concurrent identical requests | up to 10 calculations/reads | 1 calculation, 1 canonical read | `test_chart_derived_service.py` |
| Warm derived request | route-dependent read/requeue | 0 provider reads, 0 calculation | `test_chart_derived_service.py` |
| Frontend derived cache | unbounded maps | 64 entries per kind, expired sweep + LRU | chart runtime tests |
| Frontend candle runtime cache | unbounded symbol/interval keys | all active keys + 8 inactive keys | chart runtime tests |
| Bidask 1m/10m/1h switching | 3 identical intraday requests | 1 intraday request | Playwright request assertion |

## Visual Equivalence

- Desktop: 8 screenshots at 1440x900 passed.
- Mobile: 8 screenshots at 390x844 passed.
- Modes: candle, line, ohlc, bidask 1m/10m/1h passed.
- Layers: fixed SMA 5/20/60, optional EMA/RSI, candle volume profile passed.
- Tiled workspace: chart, compare, order-flow panel and bottom nav/chrome overlap assertions passed.
- Pixel threshold: `maxDiffPixelRatio=0.001`, animations/transitions disabled, fixture API/WS only.
- Playwright result: 6/6 tests passed; every canvas also passed nonblank pixel checks.

## Validation

| Command | Result | Notes |
| --- | --- | --- |
| Python 3.12 `compileall` | pass | root `.venv`, empty local Alpaca credentials, AWS metadata disabled |
| market-data unittest | pass | 328 passed, 6 skipped |
| api-server unittest | pass | 183 passed |
| frontend `npx tsc -b` | pass | no diagnostics |
| frontend `npm run build` | pass | existing 500kB chunk warning only |
| `npm run test:chart` | pass | chart runtime tests passed |
| `npm run test:chart-visual` | pass | 6 passed; initial sandbox bind denial rerun with approved local server permission |
| `docker compose config` | pass | final service graph has no derived worker |
| `docker compose build` | pass | all images built after final source changes |
| `kubectl kustomize` base/aws/aws-incluster-app | pass | no orphan deployment/patch |
| chart contract checker | pass | DDL, topic, processed-topic, TTL, lifecycle contracts reconciled |
| `git diff --check` | pass | clean |
| Terraform fmt/validate | not run | Terraform CLI unavailable; no apply attempted |

## Operator After-Deploy Slot

The agent did not run these commands against production. Full procedure:
`docs/CHART_DATA_CONTRACTS.md`.

```text
Observation window/date:
Deployed commit:
Active symbol/session count:
Redis INFO commandstats delta:
Redis chart key cardinality/memory:
Kafka messages/bytes/lag:
Retired topic consumers (expected none):
ClickHouse rows/bytes/parts by table/day:
S3 PUT/LIST/GET, bytes, objects by final/final-v2/raw/raw-v2:
Derived calculate/cache-hit/singleflight/failure counters:
Desktop/mobile visual smoke:
Anomalies and rollback decision:
```

## Rollback Readiness

- Code rollback: deploy the preceding image set; old broker topics and existing old tables were not deleted.
- S3: switch to `dual` while comparing v1/v2 or restore the preceding v1 writer.
- Redis: cache keys are versioned and TTL-bound; no migration is required.
- ClickHouse/S3 retention: extend/disable before age-out; no expiry happened during this Goal.
- Irreversible production expiry/deletion performed: none.
- Local rollback drill: mode/equivalence/unit tests passed; no live rollback command was run.
