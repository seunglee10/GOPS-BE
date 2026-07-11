# Postgres Platform Contract

PostgreSQL owns transactional latest-state projections. Canonical market time
series remain in ClickHouse.

```text
docker-compose postgres
systems/order/jobs/migrations
systems/agent-orchestration/jobs/chart-asset-migrations
```

## Chart Analysis Assets

`chart_assets.analysis_assets` stores exactly one latest JSONB row per
`(symbol, interval)`. It stores no candle, rejected-candidate ledger, prompt,
response, or build log. The chart asset public routes keep the same shape while
`CHART_ASSET_STORAGE_MODE` controls a guarded migration:

```text
clickhouse
dual_clickhouse_read
dual_postgres_read
postgres
```

Dual modes write both stores and read only the named primary. A primary write
failure is fatal; a shadow failure is a visible warning and blocks cutover.
Both stores arbitrate by `generated_at` and canonical payload digest. A
conditional no-op that leaves primary/shadow payloads different is also a
visible shadow warning; a delayed older build is never silently treated as a
successful parity write.
Delete must complete in both stores. Before changing the read primary, run the
one-shot schema/sync job with build/delete maintenance enabled and require exact
`symbol + interval + canonical payload digest` parity. Keep ClickHouse shadow
writes for one observation release before `postgres`; table removal is a later
operator migration. The sync/verify runner fails closed unless the builder
Deployment is scaled to zero and all builder Pods have terminated.

Local Compose exposes the migration job through the `chart-assets-pg` profile.
For local sync/verify, first stop `chart-asset-builder`, set
`CHART_ASSET_STORAGE_MAINTENANCE=true`, and pass the selected
`CHART_ASSET_MIGRATION_ACTION` to the one-shot Compose service.
EKS uses `infra/k8s/base/job-chart-asset-migrations.yaml` and
`scripts/aws/run-chart-asset-migrations-job.sh`; runtime never creates the schema.
The one-shot `verify` action idempotently reapplies `CREATE ... IF NOT EXISTS`
before its read-only parity comparison; `upsertedRows` reports actual conditional
writes and `attemptedRows` reports the source rows examined by sync.
AWS/EKS can point `DATABASE_*` or `DATABASE_URL` at the in-cluster database or
RDS. Never commit real passwords or connection strings.
