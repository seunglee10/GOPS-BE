# Workstream 05: Derived Calculation Ownership

## Goal

Give optional indicators and candle-based volume profile one request-time server owner and remove
Kafka/ClickHouse request-artifact work that has no independent reader.

## Structural problem

Volume profile uses `ChartDerivedDataClient` and can return pending after Kafka enqueue
(`systems/market-data/shared/alfaka/serving/chart_derived_data.py:316-358`), while indicators compute
synchronously in the API (`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:242-308`).
The worker writes Redis and ClickHouse artifacts (`systems/market-data/pods/chart-derived-data-worker/main.py:87-105`).
This mixture is historical, not based on readership or cost: both results depend on a user's visible
range/parameters, and both are consumed through existing API routes.

Fixed SMA 5/20/60 is excluded from this Goal. It remains candle-owned because the default chart
requests and renders it (`apps/gops-frontend/src/chart/indicatorLayerPolicy.ts:3-9`).

## Alternatives

1. Move indicators into the existing Kafka worker. Consistent, but adds queue/poll latency to cheap
   bounded calculations and preserves artifact writes.
2. Move VP into a shared synchronous server service with indicators; Redis TTL cache/singleflight
   only. Cost follows actual requests and results remain available outside the browser.
3. Compute both in the browser. Removes server CPU but makes the browser the only calculation
   contract and duplicates work per client.

## Decision and tradeoffs

Choose option 2. Existing candle/range caps bound work. The API becomes responsible for request CPU,
so cache and operation-count tests are mandatory. In return the result is immediately ready, there
is no queue retry loop, and Kafka/ClickHouse writes become zero for these requests.

## Change specification

- Add one shared `DerivedCalculationService` behind the current indicator and VP routes.
- Read candles only through workstream 04's canonical facade.
- Preserve all numerical algorithms, ordering, target-bin limits, warmup behavior, `dataStatus`,
  and result arrays; move code, do not rewrite formulas.
- Use complete request identity including symbol, interval, from/to, layers, limit, target bins,
  price bin size, `priceMin`, `priceMax`, and calculation version.
- Cache successful results in Redis with current kind TTLs. Use `SET NX EX` singleflight; a waiter
  either observes the result within 500 ms or performs a bounded fallback calculation.
- Keep client retry handling for one compatibility release, then remove dead retry timers after
  `pending` is no longer emitted.
- Add code-level counters for calculate/cache-hit/singleflight-wait/failure and provider read count.
- Add the new service/equivalence suite as
  `systems/market-data/tests/test_chart_derived_service.py`; retain worker tests through Release B.

### [CONTRACT-CHANGE CC-3] migration

1. Release A: `CHART_DERIVED_EXECUTION_MODE=worker|inline-shadow|inline`, default `worker`. Shadow
   computes inline without serving/writing and compares normalized payloads in tests/local fixtures.
2. Release B: default `inline`; preserve worker fallback behind the flag. Responses use
   `derived.state=ready|failed` and `derived.source=api-compute|redis` instead of queued/pending/worker.
3. Release C: remove enqueue and scale the worker to zero after an operator confirms no external
   consumers of `market.chart-derived.requests.v1`/DLQ.
4. Workstream 08 removes topic inventory and fresh-install `chart_derived_artifacts` DDL after its
   existing TTL drains. Existing production tables are not dropped by the agent.

Public route paths and result data are unchanged. The transition metadata values above are an API
response contract change and require approval with CC-3.

## Query contract evaluation

The route remains the durable query boundary, so a future non-browser consumer is better served
than by frontend calculation. There is intentionally no new artifact lookup API or analysis API.

## Acceptance criteria

1. Old worker and new inline calculations are equal for all fixture intervals/layers/ranges after
   normalizing generated time and `derived` transition metadata.
2. Cold request performs at most one canonical candle read (two for documented indicator warmup +
   visible range); warm request performs zero provider reads.
3. Ten concurrent identical requests calculate once and return equal payloads.
4. VP and indicator requests produce zero Kafka sends and zero ClickHouse artifact inserts in inline mode.
5. Result size/order/numbers and every visual snapshot are unchanged.
6. Worker fallback can be restored by one env change until Release C.

## Validation commands

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_chart_derived_service.py'
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/market-data/tests -p 'test_chart_derived_data_worker.py'
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_market_data_query.py'
(cd apps/gops-frontend && npm run test:chart)
(cd apps/gops-frontend && npm run test:chart-visual)
git diff --check
```

Then run the full gate.

## Rollback

Set execution mode to `worker`; topics/table remain present until the later cleanup Goal. Redis
cache keys are versioned, so rollback does not reinterpret inline entries.

## Not doing

- No analysis-engine contract or precomputed universe-wide indicator store.
- No formula or visual change.
- No worker/topic/table removal in the same release that changes serving mode.
