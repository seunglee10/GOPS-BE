# Workstream 04: Canonical Query and Fill Facade

## Goal

Make every existing candle consumer storage-first and deduplicate missing-range repair across API
replicas, without changing routes or visible data.

## Structural problem

The primary candle route uses Redis/ClickHouse plus fill
(`systems/api-server/pods/api-server/gops-backend/app/market_data/query/service.py:67-121`), but compare
calls Alpaca directly per symbol (`app/market_data/compare/service.py:48-124`) and agent chart context
reads ClickHouse directly (`systems/market-data/shared/alfaka/serving/provider.py:224-238`). Background
fill deduplication is process-local (`app/market_data/fill/service.py:53-55,423-464`). Therefore cost
and fallback semantics depend on which route and API replica serves the same candle request.

## Alternatives

1. Keep route-specific adapters and add caches to each. Fast locally, but perpetuates inconsistent
   fallback and duplicate calls.
2. Extract an internal canonical candle facade used by current services. Centralizes policy without
   a new public API.
3. Route internal calls over HTTP to `/api/charts/candles`. Enforces a boundary but adds network
   hops, auth coupling, and self-call failure modes.

## Decision and tradeoffs

Choose option 2. The facade owns normalization, Redis -> ClickHouse -> bounded foreground fill ->
background S3/Alpaca repair, source trace, and persistence. Existing route services retain response
formatting. The main tradeoff is a larger shared internal dependency; focused adapter tests constrain it.

## Change specification

- Extract `CanonicalCandleQuery` from current query/fill behavior under the market-data serving
  boundary.
- Keep `MarketDataQueryService.candle_snapshot` as the public response assembler.
- Change compare to request canonical bars first. On a genuine miss, use the same bounded foreground
  policy and persist results; fetch at most three compare symbols concurrently.
- Change agent chart context and optional derived calculations to use the facade, never direct
  ClickHouse calls.
- Add Redis `SET NX EX` singleflight keyed by normalized symbol/source interval/range for background
  fill. Keep the local set only as an in-process fast path.
- Store terminal fill state long enough for other replicas to suppress duplicate work; lock expiry
  permits recovery after a crashed owner.
- Make the test environment force local-empty credentials and fail on any unmocked Alpaca, AWS
  Secrets Manager, or network call.
- Add focused compare/facade cases in `systems/api-server/tests/test_chart_compare.py`; retain broad
  route regression cases in `test_market_data_query.py`.

## Query contract evaluation

No public API, Kafka, Redis history, or DB schema change. Compare items, timestamps, normalization,
warnings, colors, and base calculations must be byte-equivalent after volatile `asOf/cache` fields
are normalized. Source-trace internals may add fields only where current schemas allow them.

## Acceptance criteria

1. Candle, compare, agent context, and derived callers all use one facade in source assertions.
2. A Redis/ClickHouse hit causes zero Alpaca calls for every caller.
3. A compare miss makes one bounded fill per symbol and writes canonical stores before reuse.
4. Two service instances requesting the same missing range enqueue exactly one background repair.
5. Lock-owner failure becomes retryable after TTL; no permanent stuck state.
6. Full API tests run with a network-deny fake and print no Secrets Manager/Alpaca attempt.
7. Existing route payload and visual golden tests pass.

## Validation commands

```bash
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_market_data_query.py'
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_chart_compare.py'
PYTHONPATH=systems/market-data/shared:systems/api-server/pods/api-server/gops-backend .venv/bin/python -m unittest discover -s systems/api-server/tests
(cd apps/gops-frontend && npm run test:chart-visual)
git diff --check
```

Then run the full gate.

## Rollback

Route compare/agent-context adapters back to their previous implementations and disable distributed
singleflight with one env flag. Canonical data written during the Goal is valid and needs no cleanup.

## Not doing

- No new analysis/agent API.
- No fake local market runtime.
- No change to foreground fill caps or candle visual semantics without a separate measured proposal.
