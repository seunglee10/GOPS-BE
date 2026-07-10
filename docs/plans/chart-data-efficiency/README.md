# Chart Data Efficiency Plan

Status: proposal. Production code changes require explicit approval and a new Goal run.

## Diagnosis in one page

The candle/bar/line result is stable and remains the visual baseline. The main problems are
outside the axes and renderer:

- the stream processor keeps several session-growing dictionaries and still builds a tick-based
  volume profile that no active API reads;
- the WebSocket hub combines pub/sub with a full Redis live-state poll every 250 ms;
- compare bypasses the canonical Redis/ClickHouse/S3 candle path and calls Alpaca directly on a
  cache miss, while API background-fill deduplication is process-local;
- optional indicators, candle volume profile, and order flow use three different ownership models;
- realtime S3 buffering is keyed by symbol, so one 502-symbol 1-minute wave can produce 1,004
  PUTs including manifests;
- ClickHouse tick tables and raw S3 have no retention bound, and the two ClickHouse DDL copies
  and two Kafka topic inventories disagree.

Evidence and the prior order-flow implementation audit are in
[`00-diagnostic-report.md`](00-diagnostic-report.md). The target placement rules are in
[`01-design-principles-and-placement.md`](01-design-principles-and-placement.md).

## Fixed decisions

1. **Visual output is invariant.** No workstream changes candle/bar/line/bidask geometry, time or
   price axes, zoom, colors, or visible layer results.
2. **No core tuning is proposed.** There is no `[CORE-TUNING]` item in this plan. If implementation
   exposes an axis defect, stop and write a separately approved addendum.
3. **Kafka transports facts and shared realtime events.** It is not a request queue for bounded,
   user-parameterized chart calculations.
4. **Redis is bounded hot state and singleflight only.** Every chart key has a TTL, cardinality cap,
   or both. Redis is not durable history.
5. **ClickHouse is the canonical query store.** Closed candles are durable; raw ticks are bounded
   intermediate evidence for order-flow rollup and diagnostics.
6. **S3 is rebuild storage.** Historical/backfill candle objects keep the current deterministic
   manifest layout. High-rate realtime and raw archives move to time-windowed shards.
7. **Derived placement follows readership.** Fixed SMA 5/20/60 stays with candles because every
   default chart reads it. Optional indicators and candle volume profile are calculated on demand
   by one server-side service and cached briefly. Order-flow live minutes remain stream-computed;
   daily profiles remain EOD batch output.
8. **One internal read path per fact.** Candles, compare, chart context, and derived calculations
   use the same canonical candle facade. Existing public routes remain the access boundary.

These decisions intentionally supersede `docs/CHART_DATA_REBUILD_PLAN.md:225-232`, which assigns
all optional derived work to a Kafka worker and ClickHouse artifacts. Current code already violates
that rule for indicators (`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:242-308`),
and no non-API consumer justifies permanent request-hash artifacts.

## Approval gates

The following items must not be implemented until the user approves this plan:

| ID | Change | Marker | Migration owner |
| --- | --- | --- | --- |
| CC-1 | Stop the unused `volume-profile:{symbol}:1m:live` writer and retire its Redis read contract | `[CONTRACT-CHANGE]` | WS02 + WS07 |
| CC-2 | Stop publishing `market.layer.candles.live.v1`; Redis pub/sub remains the live delivery contract | `[CONTRACT-CHANGE]` | WS03 + WS07 |
| CC-3 | Move optional derived calculation from Kafka/ClickHouse artifacts to synchronous API compute + Redis cache; retire two derived topics/table | `[CONTRACT-CHANGE]` | WS05 + WS07 |
| CC-4 | Add `final-v2`/`raw-v2` sharded S3 layouts with dual-read migration | `[CONTRACT-CHANGE]` | WS06 |
| CC-5 | Add 21-day TTL to `trade_ticks`/`quote_ticks`; stop creating legacy VP/artifact tables after drain | `[CONTRACT-CHANGE]` | WS07 |
| CC-6 | Remove the release-1 legacy order-flow Redis hash fallback after a compatibility window | `[CONTRACT-CHANGE]` | WS07 |

No AGENTS.md-protected route is removed or renamed. CC-3 preserves the indicator and volume-profile
route parameters and result data; only `derived.state/source` transition metadata changes as
specified in workstream 06.

## Workstreams and order

Priority is based on expected load reduction divided by implementation difficulty. Execution order
also respects migration dependencies.

| Order | Goal | Effect | Difficulty | Why now |
| --- | --- | ---: | ---: | --- |
| 1 | [`02-workstream-visual-baseline.md`](02-workstream-visual-baseline.md) | safety 5 | 2 | Freeze pixel and payload behavior before data changes |
| 2 | [`03-workstream-processor-state.md`](03-workstream-processor-state.md) | 5 | 2 | Remove per-trade dead writes and bound memory |
| 3 | [`04-workstream-realtime-delivery.md`](04-workstream-realtime-delivery.md) | 4 | 2 | Replace 250 ms Redis scans and duplicate REST refresh |
| 4 | [`05-workstream-query-facade.md`](05-workstream-query-facade.md) | 4 | 2 | Stop direct/duplicate fills without changing routes |
| 5 | [`07-workstream-s3-layout.md`](07-workstream-s3-layout.md) | 5 | 4 | Remove symbol-linear object creation with dual read |
| 6 | [`06-workstream-derived-data.md`](06-workstream-derived-data.md) | 3 | 3 | Unify calculation ownership after facade is stable |
| 7 | [`08-workstream-retention-contracts.md`](08-workstream-retention-contracts.md) | 4 | 2 | Apply retention and remove drained compatibility paths |

Each file is an independent Goal specification. A Goal may be rolled back without reverting a
previous completed Goal. Workstream 08 is deliberately last because it removes drained contracts.

## Global guardrails

- Never connect to production or attempt a production baseline. Alpaca and Secrets Manager must be
  replaced by test doubles in local validation.
- Never inject fake candles or ticks into a local runtime. Renderer fixtures exist only inside the
  test process through network/component test doubles.
- Keep the current public chart routes and payload data unless a listed `[CONTRACT-CHANGE]` is
  separately approved.
- Do not touch order/KIS behavior, agent analysis design, or build APIs for a future analysis engine.
- Do not push. Use the repository-root Python 3.12 `.venv`.
- Preserve the current visual snapshot. Any screenshot change blocks the Goal even when data tests
  pass.

## Full validation gate

Run in this order after every Goal, selecting the relevant tests first and the full gate before
completion:

```bash
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m compileall -q systems scripts/local
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/market-data/tests
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/api-server/tests
(cd apps/gops-frontend && npx tsc -b)
(cd apps/gops-frontend && npm run build)
(cd apps/gops-frontend && npm run test:chart)
(cd apps/gops-frontend && npm run test:chart-visual)
docker compose config
docker compose build
git diff --check
```

The future visual command is introduced by workstream 02. During that Goal, run the existing gate
first, then add the command and freeze its baseline.

## Production observations delegated to the operator

There is no pre-change production snapshot. Acceptance uses code-level command/object/state-count
tests plus one operator-run market-hours observation after deployment:

- Redis: `INFO commandstats`, active chart session count, and chart-key cardinality;
- Kafka: per-topic bytes/messages and consumer lag, including confirmation that CC-2/CC-3 topics
  have no active consumers before producers are disabled;
- ClickHouse: rows, compressed bytes, and parts by chart table/day;
- S3: PUT/LIST/GET counts, bytes, and object count by `final-v1`, `final-v2`, `raw`, and `raw-v2`.

The agent records procedures and empty result slots, but never runs them against production.
Use [`IMPLEMENTATION_REPORT_TEMPLATE.md`](IMPLEMENTATION_REPORT_TEMPLATE.md) for every Goal.
