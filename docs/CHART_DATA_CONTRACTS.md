# Chart Data Contracts And Operator Runbook

This is the durable storage/query contract for the current chart pipeline. Public
chart API and WebSocket payloads remain documented in `CHART_DATA_REBUILD_PLAN.md`.
No command in this runbook was executed against production by Codex.

## Contract Matrix

| Fact | Producer | Transport/storage | Reader | Retention/bound | Fallback |
| --- | --- | --- | --- | --- | --- |
| raw trade/quote | Alpaca ingestors | input Kafka; ClickHouse ticks; S3 raw-v2 | processors; EOD/audit | Kafka 2h; ClickHouse 21d; S3 30d | none; every accepted tick is evidence |
| closed candle | market processor/fill | interval Kafka; Redis recent; `chart_candles`; final/final-v2 | canonical candle facade | Redis 120 rows; ClickHouse/S3 no expiry | Redis -> ClickHouse -> bounded fill |
| live candle | market processor | Redis `live:candle:*`; `market.events` pub/sub | API/WS | 180s default + stale guard | five-second Redis recovery |
| fixed SMA 5/20/60 | candle processor/provider | candle fields | default chart | same as candle | canonical recompute after correction |
| optional indicator | API derived service | Redis versioned result + lock | indicator route | 300s result; 30s lock | bounded local calculation after 500ms wait |
| candle volume profile | API derived service | Redis versioned result + lock | volume-profile route | 30s result; 30s lock | bounded local calculation after 500ms wait |
| live order flow | market processor | `order-flow:*:minutes` + `live-minute` | order-flow intraday API | 86400s + 300s | no legacy hash fallback |
| daily order flow | EOD rollup job | `order_flow_profile_daily` | daily API/panel/audit | no deletion TTL | manual rerun from retained ticks |
| realtime event | processors | Redis `market.events` pub/sub | WebSocket hub | ephemeral | one snapshot + five-second recovery |

Processed S3 consumes closed candles and events only. Tick evidence belongs to
raw-v2 and bounded ClickHouse tables. V2 realtime objects use one-minute windows
and 32 deterministic symbol shards; historical/backfill v1 manifests remain
valid and dual readers support migration.

## Repository Gate

```bash
PYTHONPATH=systems/market-data/shared .venv/bin/python scripts/local/check-chart-data-contracts.py
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_storage_contracts.py'
docker compose config
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/aws
```

The checker compares the two Kafka inventories and normalized market-data DDL,
checks tick/canonical TTL rules, aligns processed S3 topic lists, and validates
the Terraform raw lifecycle.

## Before Deploy

1. Record the candidate commit and run the repository gate. Do not use Alpaca or
   inject market fixtures into a local runtime.
2. Inspect all Kafka consumer groups for the retired live-candle topic and the
   two retired chart-derived request/DLQ topics. Continue only when no external
   consumer depends on them.
3. Deploy the producer/API/worker-removal release. Do not delete broker topics;
   retention drains them and deletion remains a separate operator decision.
4. Verify indicators and candle volume profile return `derived.state=ready` and
   `derived.source=api-compute|redis` with no pending response.
5. Review `terraform plan` for exactly two chart raw prefix rules at 30 days and
   no final/final-v2 expiration. Apply only through the normal infrastructure
   workflow.
6. Review ClickHouse parts/bytes and backup availability, then apply
   `scripts/local/migrate-chart-tick-retention.sql` through the approved
   maintenance path. No table drop is part of this migration.

Fresh installs no longer create the retired tick volume-profile or derived
request-artifact tables. Existing tables are intentionally left in place; their
later drop requires a separately approved operator change.

## After Deploy

Perform one market-hours observation and paste results into the implementation
report.

```text
date/time and commit:
active chart sessions:
Redis INFO commandstats before/after observation:
Redis chart key cardinality and memory:
Kafka messages/bytes/lag by active chart topic:
retired topic consumer groups (expected none):
ClickHouse rows/compressed bytes/parts by chart table and day:
S3 PUT/LIST/GET and object count by final, final-v2, raw, raw-v2:
API derived calculate/cache-hit/singleflight/failure counters:
visual smoke result (desktop/mobile):
notes:
```

## Rollback

- Redeploy the preceding image set while the old broker topics and existing
  tables still exist; no automated deletion in this change prevents rollback.
- Set `S3_REALTIME_LAYOUT_MODE=dual` while comparing v1/v2 reconstruction, or
  restore the preceding v1 writer release if required.
- Extend or remove the ClickHouse TTL before rows age out. Expired rows require
  restore from retained raw S3 evidence.
- Revert Terraform lifecycle before the 30-day age boundary if the raw repair
  window must be extended.
