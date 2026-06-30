# AWS Market Data Runtime Parity And Reset Runbook

이 문서는 AWS/EKS 실제 운영 환경의 차트 데이터 상태를 로컬에서 검증한 market-data 계약과 맞추기 위한 실행 기준이다.
Git push는 reset을 자동 실행하지 않는다. AWS reset은 운영자가 kube context, 대상 namespace, 영향 범위를 확인하고 별도로 승인한 뒤 수동 실행한다.

## 1. Required Parity Contract

로컬 기준으로 검증한 차트 데이터 계약은 AWS 생태계에서도 같아야 한다.

- ClickHouse chart-serving data는 로컬과 AWS가 같은 canonical 기준이어야 한다.
- S3 canonical parquet/manifest는 ClickHouse rebuild source이며, 로컬과 AWS가 같은 bucket/prefix 계약을 보아야 한다. 현재 S3는 같은 저장소를 사용한다고 가정하되, 실제 실행 전 object/manifest audit로 확인한다.
- Redis chart/live/backfill keyspace는 로컬 reset 후 상태와 AWS reset 후 상태가 같은 의미를 가져야 한다. Redis는 durable historical source가 아니라 realtime/latest/recent/cache/queue state다.
- Kafka chart topics와 consumer group 상태는 로컬과 AWS가 같은 message contract를 사용해야 한다. 단, local broker 주소와 AWS in-cluster broker 주소는 달라도 된다.
- endpoint, password, Secret 이름처럼 환경별 값은 달라도 되지만, market-data behavior는 같아야 한다.
- AWS apply-only 배포에서 과거 `alfaka-alpaca-ingestor` Deployment가 남아 있으면 안 된다. 현재 active ingestor는 `alfaka-alpaca-ingestor-sip`와 `alfaka-alpaca-ingestor-boats`뿐이어야 한다. Legacy `alfaka-alpaca-ingestor`와 과거 `alfaka-alpaca-ingestor-iex` Deployment는 absent 또는 `replicas=0`이어야 한다.
- Redis는 chart/live/backfill runtime state이며, S3/ClickHouse가 durable historical source다. Redis background snapshot 실패가 backfill/status writes를 막지 않도록 compose와 in-cluster Redis는 `--appendonly yes --save "" --stop-writes-on-bgsave-error no` 계약을 사용한다.
- S3 raw/live/final prefixes must keep their roles separate. Raw/live append objects are evidence and replay inputs; they are not direct chart-serving truth. ClickHouse serving should be rebuilt from final canonical parquet/manifest or from bounded materialization/compaction that dedupes by logical candle identity.

AWS runtime must use these market-data contract values:

```text
ALPACA_UNIVERSE=gops20
ALPACA_UNIVERSE_REGISTRY_PATH=
ALPACA_FEED_PROFILES=sip,boats
ALPACA_ENFORCE_FEED_SESSION_WINDOW=true
ALPACA_SESSION_IDLE_POLL_SECONDS=60
HOT_TIER_SIZE=10
HOT_TIER_FALLBACK_SCAN_LIMIT=20
S3_PROCESSED_FORMAT=parquet
S3_HISTORICAL_RAW_PARTITION_MODE=chunk
S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT=compact
HISTORICAL_ADJUSTMENT=split
ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT=false
CLICKHOUSE_REQUIRE_CANONICAL_CANDLES=true
S3_REQUIRE_CANONICAL_PROCESSED_CANDLES=true
BACKFILL_INITIAL_LOAD_1M_MIN_START=2023-07-01T00:00:00Z
S3_RAW_PREFIX=market-data/rebuild-20260701/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260701/final
S3_LIVE_PREFIX=market-data/rebuild-20260701/live
S3_MANIFEST_PREFIX=market-data/rebuild-20260701/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260701/final
```

Forbidden active runtime values:

```text
sp500
semiconductor-100
HOT_TIER_SIZE=20
S3_PROCESSED_FORMAT=jsonl
HISTORICAL_ADJUSTMENT=raw
BACKFILL_INITIAL_LOAD_1M_MIN_START=2025-04-01T00:00:00Z
```

## 2. Reset Scope

Target namespace:

```text
alfaka-market-data
```

ClickHouse reset tables:

```text
market_data.chart_candles
market_data.volume_profile_bins_1m
market_data.trade_ticks
market_data.market_status_events
market_data.load_audit
```

ClickHouse preserve tables:

```text
market_data.symbols
market_data.news_articles
```

Redis scan-delete patterns:

```text
price:*
candle:*
candles:*
market.events*
active:charts*
hot:*
market:status*
volume-profile:*:1m:live
backfill:*
pipeline:health:*
symbols:metadata:*
symbols:search:index
```

Optional Redis reset only for a complete demo initialization:

```text
watchlist:symbols
```

Forbidden reset actions:

```text
FLUSHALL
DROP DATABASE market_data
PVC deletion
S3 canonical prefix deletion
auth/session/order/agent Redis key deletion
```

## 3. Execution Order

1. Confirm the current kube context and namespace.
2. Confirm AWS runtime config:
   - ConfigMap `alfaka-market-data-config`
   - `/health/config` redacted response
   - sampled running pod env
   - `alfaka-alpaca-ingestor` legacy Deployment is absent or has `replicas=0`
   - active ingestor Deployments are only `alfaka-alpaca-ingestor-sip` and `alfaka-alpaca-ingestor-boats`
   - stale `alfaka-alpaca-ingestor-iex` is absent or has `replicas=0`
3. Scale down market-data writers/workers:
   - `alfaka-alpaca-ingestor-sip`
   - `alfaka-alpaca-ingestor-boats`
   - stale `alfaka-alpaca-ingestor-iex`, only if it is still present and not already `replicas=0`
   - legacy `alfaka-alpaca-ingestor`, only if it is still present and not already `replicas=0`
   - market processor
   - processed S3 sink
   - raw S3 archive
   - ClickHouse loader
   - backfill worker
4. Snapshot current ClickHouse row counts by table, interval, canonical version, and adjustment.
5. Snapshot current Redis chart/backfill/live key counts by pattern.
6. Snapshot Kafka chart topic lag and consumer group offsets.
7. Truncate only the ClickHouse chart reset tables.
8. Scan-delete only the Redis chart/market-data patterns listed above.
9. Do not delete S3 canonical parquet or manifest data.
10. Deploy or confirm the code version that contains the current market-data contract.
11. Materialize S3 canonical parquet into ClickHouse first.
12. Use Alpaca initial-load/backfill only for target-range data that is missing from S3.
13. For regular-session sparse chart gaps, queue only the API-reported `coverage.gapRanges`; do not enqueue a full-range forced backfill from the browser.
14. Confirm `BACKFILL_ACTIVE_STALE_SECONDS` and `BACKFILL_MAX_GAPFILL_1M_RANGE_HOURS` are present in the running ConfigMap.
15. Scale market-data writers/workers back up.
16. Recheck ClickHouse, Redis, Kafka, API, and browser smoke.

## 4. Post-Reset Verification

Required checks:

- ClickHouse chart reset tables are empty immediately after reset.
- Redis chart/market-data key count is zero or the expected minimal post-reset state.
- Redis accepts writes after reset. If Redis reports `MISCONF stop-writes-on-bgsave-error`, fix the Redis runtime config before running backfill.
- Kafka chart topic contracts exist and no stale consumer group blocks live processing.
- No legacy `alfaka-alpaca-ingestor` pod is producing raw Alpaca messages.
- S3 canonical final object and manifest audits pass.
- S3 -> ClickHouse materialize succeeds and is idempotent.
- `/health/config` has no stale market-data warnings.
- `/api/charts/candles` returns only `canonical_version=v2` and `price_adjustment=split`.
- `/api/charts/hot-symbols` returns Hot Top10 inside `gops20`.
- `/api/charts/watchlist` returns the 10-symbol first-user seed or saved user watchlist.
- Browser smoke passes for chart, Watch List, and Hot Ranking.

Representative API smoke:

```text
GET /health/config
GET /api/charts/candles?symbol=AAPL&interval=1D&limit=120
GET /api/charts/candles?symbol=NVDA&interval=1m&limit=120
GET /api/charts/hot-symbols?limit=10
GET /api/charts/watchlist
GET /api/charts/symbols?query=brk
```

On `stargops.com`, the public Ingress routes `/api` and `/ws` to the backend.
Use a backend pod/service port-forward or an internal cluster request for
`/health/config` unless the Ingress is explicitly changed to expose `/health`.

## 5. Push And Operations Rule

The dev push may include this runbook and config parity fixes.
It must not include an automatic AWS destructive reset job.

Before pushing:

```text
git diff --name-status
docker compose config --quiet
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/aws
kubectl kustomize infra/k8s/overlays/aws-incluster-app
```

After pushing, AWS reset/rebuild is a separate operator-approved action using this runbook.
