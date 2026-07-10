# Workstream 07: Retention and Contract Reconciliation

## Goal

Bound durable intermediate data, remove drained compatibility definitions, and make Redis/Kafka/
ClickHouse/S3 query contracts agree across code, local compose, K8s, and durable docs.

## Structural problem

Tick tables have no TTL (`infra/clickhouse/initdb/01-market-data.sql:7-50,329-330`), S3 has no
lifecycle (`infra/aws/terraform/main.tf:39-68`), and old VP/artifact tables remain after their
active readers are removed. Local/K8s DDL and Kafka topic inventories differ, while Redis and S3 docs
describe superseded keys/copies (`platform/redis/README.md:38-51`; `platform/s3/README.md:17-28`).
Without one contract inventory, every future consumer must rediscover the pipeline.

## Alternatives

1. Keep all history and document it. Avoids migrations but leaves unbounded storage and dead schemas.
2. Apply fixed retention and reconcile generated contracts after upstream writers/readers drain.
3. Size retention dynamically from production bytes/day. More precise, but no pre-change production
   baseline exists and waiting would preserve unbounded growth.

## Decision and tradeoffs

Choose option 2 with conservative defaults and operator-adjustable values. Twenty-one days of ticks
covers the 5-10 recent-session verification window including holiday gaps; 30 days of raw S3 gives a
longer repair window. Historical candles and daily order flow remain unbounded canonical history.

## Change specification

### [CONTRACT-CHANGE CC-5] ClickHouse

- Add `TTL event_time + INTERVAL 21 DAY DELETE` to `trade_ticks` and `quote_ticks` in both init DDLs
  and an idempotent operator migration.
- Keep `chart_candles` and `order_flow_profile_daily` without deletion TTL.
- After CC-1/CC-3 drains, stop creating `volume_profile_bins_1m` and
  `chart_derived_artifacts` in fresh environments. Do not execute `DROP TABLE` in this Goal.
- Leave unrelated local-only `agent_graph_expansions` untouched and file it with the agent-domain owner.
- Generate both DDL copies from one source or enforce normalized byte equality in CI.

### [CONTRACT-CHANGE CC-6] Redis

- Remove `order-flow:{symbol}:live` fallback and its key builder after a release with zero fallback
  counter. Keep `order-flow:{symbol}:live-minute` and `order-flow:{symbol}:minutes` TTL contracts.
- Remove old VP key/provider after CC-1's compatibility window.
- Document every chart key with owner, reader, TTL/cap, and migration version.

### [CONTRACT-CHANGE CC-2/CC-3/CC-4] Kafka and S3 contracts

- Remove drained live-candle and derived topics from the source inventory only after their producer
  releases and operator consumer checks. Broker deletion remains manual.
- Make platform and K8s topic inventories generated/equal.
- Add Terraform lifecycle: `raw/` and `raw-v2/` expire after 30 days; no expiry for final/backfill
  candle evidence.
- Align compose processed topics with K8s: candles/events only.

### Durable documentation

Update `docs/CHART_DATA_REBUILD_PLAN.md`, `platform/redis/README.md`,
`platform/clickhouse/README.md`, `platform/kafka/README.md`, `platform/s3/README.md`, market-data
README/config comments, and operator deployment docs. Include one matrix: fact -> producer -> key/
topic/table/prefix -> reader -> retention -> fallback.

Add `scripts/local/check-chart-data-contracts.py` to compare generated DDL/topic bodies while
allowing only declared environment-specific headers.
Add fixture/schema assertions in `systems/market-data/tests/test_storage_contracts.py`.

## Query contract evaluation

This Goal changes storage contracts only after readers/writers are gone. Existing API/WS payloads
remain unchanged. A future consumer gets one documented storage adapter per layer, but is expected to
use existing APIs unless it owns an internal storage adapter.

## Acceptance criteria

1. Schema tests assert 21-day tick TTL, no TTL on candles/daily OF, and local/K8s DDL equivalence.
2. Topic inventory copies are equal and contain no drained producer-only topics.
3. Redis fallback counters are zero in code-level migration tests; legacy methods/keys are absent.
4. Terraform plan/test shows 30-day raw lifecycle and no final-prefix expiration.
5. Compose and K8s processed S3 topic lists match.
6. `rg` finds legacy keys/table/topic names only in migration history and this plan.
7. Full build, test, compose, and visual gates pass.

## Validation commands

```bash
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_storage_contracts.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_orderflow_bins.py'
PYTHONPATH=systems/market-data/shared .venv/bin/python scripts/local/check-chart-data-contracts.py
docker compose config
docker compose build
git diff --check
```

Then run the full gate.

## Rollback

- Extend/disable TTL before data ages out; expired rows cannot be restored except from raw S3.
- Keep dual S3 readers and old Redis fallbacks in the preceding release until acceptance is signed.
- Restore topic producers via their flags; do not recreate or delete broker topics automatically.
- Existing old ClickHouse tables remain available because this Goal never drops them.

## Not doing

- No retention on canonical closed candles or daily order flow.
- No production DDL, S3 deletion, broker deletion, or metric collection by the agent.
- No cleanup of order/KIS or agent-owned schemas.
