# Postgres Platform Contract

PostgreSQL owns transactional latest-state projections. Canonical market time
series remain in ClickHouse.

```text
docker-compose postgres
systems/order/jobs/postgres-migrations
systems/agent-orchestration/jobs/chart-asset-migrations
```

## Chart Geometry Assets

`chart_assets.geometry_assets` stores exactly one latest Geometry JSONB payload
per `(symbol, interval)`. Runtime storage is PostgreSQL-only; there is no
ClickHouse asset table, guarded dual-write mode, or `CHART_ASSET_STORAGE_MODE`.
Canonical candles and optional repair materialization continue to live in
ClickHouse and are not copied into the asset payload.

The row keeps query/audit projections (`as_of`, `generated_at`, versions,
coverage state, drawing count, payload bytes, input/payload digests) beside the
JSONB payload. UPSERT replaces the complete payload only when `generated_at` is
newer, or when the timestamp is equal and the canonical payload digest differs.
A delayed older build is a no-op.

Geometry v6 keeps `assetVersion="geometry"` and adds optional fields under
`geometry`. It uses the existing JSONB column and the existing 0..8 drawing
constraint, so deployment does not delete rows, alter the table, or run a data
backfill. Canonical UTF-8 payloads over 64 KiB are rejected before SQL execution;
a failed build never replaces the previous successful row.

New build and refresh jobs accept only `1m` and `1D`. Existing rows for
`5m/10m/1h/4h/1W` remain readable and can be removed by explicit selected-pair
DELETE. `chart_assets.geometry_build_jobs` and `geometry_build_items` own queue,
priority, lease, bounded log, coalescing, cancel, and polling state. S&P500-wide
force refresh is not supported.

The migration jobs remain the bootstrap path for a fresh PostgreSQL database;
they are not a Geometry v6 rollout step. `CHART_ASSET_STORAGE_MAINTENANCE=true`
keeps GET available while blocking build and DELETE during an operator-owned
schema maintenance window.

AWS/EKS can point `DATABASE_*` or `DATABASE_URL` at the in-cluster database or
RDS. Never commit real passwords or connection strings.
