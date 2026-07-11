# Chart Data Operations

Use this runbook for local validation, deployment observation, recovery, and
rollback. Architecture and public contracts are in
`CHART_DATA_ARCHITECTURE.md`.

## Local Constraints

- Use repository-root `.venv` with Python 3.12.
- Do not call production or Alpaca from local validation; keys are intentionally
  absent. Use unit fixtures and `?orderFlowDemo=1` browser fixtures.
- Do not inject fake candles into a running local market pipeline.
- Do not delete broker topics, tables, S3 objects, or Redis namespaces as part
  of validation.
- Repository-root `.env.example` is the Docker Compose chart-data contract;
  `systems/api-server/.env.example` is the backend-only contract. Real `.env`
  files stay ignored. The contract checker fails when a documented tuning value
  is missing, differs from Compose defaults, or is not forwarded to a service.

## Validation Gate

```bash
export PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend
.venv/bin/python -m compileall -q systems scripts/local scripts/aws
.venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_*.py'

cd apps/gops-frontend
npx tsc -b
npm run build
npm run test:chart
npm run test:chart-visual
cd ../..

docker compose config
docker compose build
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/aws
kubectl kustomize infra/k8s/overlays/aws-incluster-app
.venv/bin/python scripts/local/check-chart-data-contracts.py
terraform -chdir=infra/aws/terraform fmt -check
terraform -chdir=infra/aws/terraform init -backend=false
terraform -chdir=infra/aws/terraform validate
git diff --check
```

If Terraform is not installed locally, record that limitation; do not replace
validation with an unreviewed remote apply. Browser visual checks cover desktop
and mobile around 640px and must not update baseline PNGs unless the visual
contract intentionally changes.

## Redis Load Evidence

The pre-change production snapshot was not collected. Unit tests with fake
clock/spy Redis are the before/after evidence; an operator performs one market-
hours commandstats sample after deployment.

| Path and fixture | Before | Current bound |
| --- | ---: | ---: |
| 5 quotes in one throttle window | 50 commands | 5: quote `SET EX` 1, global `PUBLISH` 1, health `SET EX` 3 |
| 5 live trades in one 250ms window | 10 | `HSET` + `EXPIRE` once/window |
| 8 order-flow trades across 3 minutes | 16: `HSET` + `EXPIRE` per trade | 7: live `SET EX` 4, closed-minute `ZADD` 2, session `EXPIRE` 1 |
| 5 health attempts in one 1000ms window | 30 | 3 `SET EX` once/interval |

Tests: `test_orderflow_redis_lean.py` and
`test_market_data_singleflight.py`. Redis lock release and terminal transition
use compare-and-mutate Lua; an `EVAL` failure leaves the key untouched for TTL
expiry. The legacy values are directly reproduced by
`test_reproduced_legacy_quote_baseline_counts_match_documented_table` and
`test_reproduced_legacy_trade_and_health_baselines_match_documented_table` and
`test_reproduced_legacy_order_flow_baseline_matches_documented_table`.

After deploy, paste one operator sample here or in the release record:

```text
date/time and market session:
commit/image:
instantaneous_ops_per_sec:
connected_clients:
top commandstats:
memory:
notes:
```

Commands:

```bash
redis-cli INFO commandstats
redis-cli INFO stats
redis-cli INFO memory
```

## Deployment Checks

Before rollout:

1. Confirm `platform/kafka/topics.txt` matches the K8s ConfigMap copy.
2. Confirm processor, S3 sink, and ClickHouse consumers use manual commit.
3. Confirm processed and raw S3 sinks use `S3_REALTIME_LAYOUT_MODE=v2`.
4. Run `terraform plan`; lifecycle management must remain disabled for an
   existing bucket unless the bucket owner accepts the complete lifecycle
   document.
5. Record current Redis, Kafka lag, ClickHouse insert, and S3 error dashboards.

After rollout:

1. Check raw processor and quote processor lag separately.
2. Check `putRetries`, `exactReplaySkips`, and S3 sink restarts.
3. Compare ClickHouse candle/tick freshness with Redis live timestamps.
4. Open `?orderFlowDemo=1` on desktop/mobile, then perform one operator-owned
   market-hours live check without synthetic data.
5. Run the Redis sample above and retain the result with the release.

Existing Kafka topics are not repartitioned by `--if-not-exists`. Partition or
retention changes on a live cluster require an explicit operator migration.
Current active hot topic policy is 2-hour retention for raw/layer trades and
quotes, and 1 hour for `agents.market-events.v1`, with bounded segment sizes.

## S3 And ClickHouse Recovery

Dry-run ClickHouse-to-S3 regeneration first:

```bash
PYTHONPATH=systems/market-data/shared \
python scripts/aws/regenerate-s3-candles-from-clickhouse.py \
  --dry-run --symbol AAPL --interval 1m \
  --start 2026-07-01T00:00:00Z --end 2026-07-02T00:00:00Z \
  --include-groups
```

Add `--verify-rows` to read each planned group. Remove `--dry-run` only after
reviewing group count, canonical filters, destination bucket/prefix, and stable
`--run-id`. The tool writes processed final objects and manifests; it never
converts raw backup into candles.

For an existing S3 object replay, use processed final/final-v2 only. A listed
object with `matchedRowCount=0` is a miss and must fall through to Alpaca or
fail `s3-only` mode. Object audits are written after ClickHouse insertion.

Apply the idempotent tick TTL migration after reviewing both DDL copies:

```bash
clickhouse-client --multiquery < scripts/local/migrate-chart-tick-retention.sql
```

## Terraform Lifecycle Ownership

`manage_s3_chart_data_lifecycle=false` is the safe default. Enable it only when:

- this module creates the bucket, or
- the bucket owner confirms this module represents the complete lifecycle
  document and sets `acknowledge_s3_lifecycle_document_ownership=true`.

For an externally managed bucket, its owner must add both raw-prefix expiry
rules. Never enable this module merely to append rules; the AWS lifecycle API
treats the configuration as one bucket-wide document.

## Chart Asset PostgreSQL Cutover

Keep `CHART_ASSET_STORAGE_MODE=clickhouse` until the chart-owned migration job
has created `chart_assets.analysis_assets`. Move to `dual_clickhouse_read` and
drain the builder queue. Then set `CHART_ASSET_STORAGE_MAINTENANCE=true`, restart
and verify `gops-backend` so build/delete return 503, restart the builder into its
read-only maintenance guard, and scale `deployment/chart-asset-builder` to zero.
Wait until no builder pod remains before running:

```bash
CHART_ASSET_MIGRATION_ACTION=sync CHART_ASSET_MIGRATION_PRUNE=true \
  scripts/aws/run-chart-asset-migrations-job.sh
CHART_ASSET_MIGRATION_ACTION=verify \
  scripts/aws/run-chart-asset-migrations-job.sh
```

`run-chart-asset-migrations-job.sh`는 sync/verify 전에 실행 중인 모든
`gops-backend` pod의 `CHART_ASSET_STORAGE_MAINTENANCE=true`를 직접 확인하고,
builder replica 0과 남은 pod 0을 확인한다. ConfigMap만 바꾸고 backend rollout을
생략한 상태에서는 fail closed로 실행하지 않는다.

Unset maintenance only after pair and canonical payload digest parity is 100%.
Then use `dual_postgres_read` for at least seven days/one release. Any missing,
extra, mismatched, shadow-write, 5xx, or latency regression returns the read
primary to ClickHouse. Switch to `postgres` only after the observation gate;
do not drop the ClickHouse compatibility table in the same rollout. Restore the
builder replica and restart both backend and builder only after the selected
storage mode and maintenance=false have been deployed.

## Rollback

- Application rollback: deploy the previous images/config while keeping topics,
  tables, Redis keys, and S3 objects intact.
- S3 v2 rollback: switch writers to `dual`, verify v1 manifests, then switch
  readers. Do not delete v2 evidence during rollback.
- Derived rollback: disable the new API image; cached results expire naturally.
- Redis throttling rollback: restore prior interval env values, not old key
  families or per-message writes.
- Terraform rollback: set lifecycle management false before apply. Coordinate
  bucket lifecycle restoration with the bucket owner.

Rollback is complete only when candle freshness, WS event flow, order-flow
minutes, and chart visual fixtures match the pre-rollout baseline.
