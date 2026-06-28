# Chart Data Trust Hardening Plan

## Goal

GOPS chart data must come from real Alpaca-backed runtime paths only. If data is missing, delayed, or unavailable, the UI should say so clearly instead of rendering generated candles or accepting optimistic backfill assumptions.

## Problems To Fix

- Old local Redis/ClickHouse/MinIO volumes may still contain development-generated rows from previous runs. The current runtime cannot reliably distinguish those rows after materialization, so local validation needs an explicit reset/check procedure.
- Backfill job success is not the same as stored candle coverage. `succeeded` can still mean too few rows, no rows, or a provider-permission gap.
- Derived intervals depend on source intervals: `5m/10m` need `1m`, and `1W/1M` need `1D`. Missing source coverage must be reported as source coverage, not generic chart emptiness.
- `No candle data` should only appear for terminal empty states. Queued/running backfill should remain loading/preparing.
- Runtime sample market event producers must not publish generated candles into Kafka/Redis/ClickHouse/S3.
- OpenAI failures should not create or leave stale chart previews.

## Implementation Contract

- Keep existing top-level chart response fields: `dataStatus`, `backfillStatus`, `canBackfill`, `message`.
- Add `coverage` as the detailed diagnostic object for chart data readiness.
- Do not reintroduce `isSynthetic`, dummy candles, mock LLM proposals, or frontend-generated OHLCV.
- Query-time aggregation remains V1 behavior for derived intervals, but readiness must be based on source interval coverage.
- Local data reset is explicit and developer-operated; production deletion logic is out of scope.

## Verification

- `rg` must show no runtime path that generates or publishes sample/dummy/synthetic market candles.
- Backend tests must cover insufficient coverage after successful backfill.
- Frontend tests must cover preparing vs terminal empty.
- Browser smoke must show real data when available and honest empty/loading/error states when unavailable.
