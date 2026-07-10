# Workstream 01: Visual Baseline and Client Cache Bounds

## Goal

Freeze current source-to-pixel behavior with fixture-only browser tests, then make browser caches
bounded and correctly keyed without changing any visible result.

## Structural problem

The existing chart test suite verifies algorithms and selected source patterns, but not rendered
pixels. Meanwhile derived cache maps never sweep/cap entries (`apps/gops-frontend/src/chart/cdcClient.ts:70-77,272-289`),
the VP stable key omits `priceMin/priceMax` (`apps/gops-frontend/src/chart/derivedRequestPolicy.ts:8-23`),
and chart-engine retains every visited candle key (`apps/chart-engine/src/runtime.ts:56,184-188`).
This makes later load work risky and lets navigation/panning grow browser memory.

## Alternatives

1. Manual screenshots only. Low setup cost, but not a repeatable acceptance gate.
2. Playwright screenshots against fixture-intercepted API calls. Repeatable and exercises the real
   app/renderer without injecting fake data into local runtime.
3. Canvas unit snapshots only. Fast, but misses layout overlap and responsive composition.

## Decision and tradeoffs

Choose option 2 plus existing canvas/runtime unit tests. Add a test-only Playwright server where
route handlers return static candle, indicator, VP, compare, and order-flow fixtures. Do not enable
demo data or publish fixture messages into Kafka/Redis. Browser snapshots cost CI time and require a
pinned Chromium version; that cost is accepted because visual identity is a hard requirement.

## Change specification

- Add `test:chart-visual` and Playwright as a dev-only dependency.
- Capture 1440x900 and 390x844 snapshots for candle, line, ohlc, bidask 1m/10m/1h, fixed SMA,
  optional indicators, candle VP, compare, and tiled order-flow panel.
- Assert no overlap with bottom command bar, side rail, preset dock, hover controls, or panel edges.
- Pin timezone, locale, device scale, fonts, animation clock, and fixture timestamps.
- Use a zero semantic-diff rule for chart geometry and a <=0.1% antialias pixel allowance.
- Include `priceMin` and `priceMax` in the VP stable key and unit-test two vertical ranges with the
  same time range.
- Replace derived maps with 64-entry LRU caches that sweep expired entries on get/set.
- Keep active candle history intact; retain the active key plus eight most-recent inactive
  symbol/timeframe keys in chart runtime.

## Query contract evaluation

No public query contract changes. Test interception is confined to the Playwright process. Cache
identity becomes equal to the existing request identity, so this fixes rather than changes results.

## Acceptance criteria

1. Baseline snapshots are captured before any later workstream code change.
2. Every listed viewport/chart/layer snapshot passes after cache changes.
3. A changed vertical range causes a VP miss; an identical request deduplicates in flight.
4. More than 64 derived identities and nine candle identities do not grow retained maps further.
5. No runtime demo flag, Kafka message, Redis write, or Alpaca call is used by the visual test.

## Validation commands

```bash
(cd apps/gops-frontend && npx tsc -b)
(cd apps/gops-frontend && npm run build)
(cd apps/gops-frontend && npm run test:chart)
(cd apps/gops-frontend && npm run test:chart-visual)
git diff --check
```

Then run the full gate in `README.md`.

## Rollback

Revert cache implementations and the visual-test dependency together; retain generated baseline
images only if the old runtime still passes them. No backend rollback is involved.

## Not doing

- No `[CORE-TUNING]`: axes, zoom, viewport defaults, and render geometry are unchanged.
- No visual redesign or token change.
- No local `orderFlowDemo` runtime as a test data source.
