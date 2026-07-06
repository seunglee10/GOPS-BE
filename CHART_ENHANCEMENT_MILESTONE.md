# Chart Enhancement Milestones

이 문서는 Codex Goal 모드가 차트 고도화를 안정적으로 구현하기 위한 실행 마일스톤이다.
제품/기술 결정은 [CHART_ENHANCEMENT_ROADMAP.md](./CHART_ENHANCEMENT_ROADMAP.md)를 source of truth로 사용한다.

Goal 모드 구현자는 두 문서를 함께 읽고, 아래 마일스톤 순서대로 진행한다.
각 마일스톤은 이전 마일스톤의 산출물을 전제로 한다.

## Execution Rules

- Do not implement out-of-scope roadmap items.
- Do not fabricate market candles, profile data, footprint data, trades, or quotes.
- Keep `GET /api/charts/candles` compatible.
- Keep existing drawing tools and WebSocket candle updates working.
- Prefer small, verified steps over broad rewrites.
- Use existing project structure:
  - frontend: `apps/gops-frontend`
  - chart engine: `apps/chart-engine`
  - backend API: `systems/api-server`
  - market-data shared code: `systems/market-data/shared`
  - storage/platform contracts: `platform`, `infra`
- After each milestone, run the milestone checks before moving on.
- Browser verification is required for user-visible chart changes.

## Common Verification Commands

Use these commands as the baseline verification set. Run the relevant subset at each milestone and the full set in M6.

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/market-data/tests
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
docker compose config --quiet
git diff --check
```

For browser checks, run the frontend dev server and verify with a real browser or Playwright-style browser control.

```sh
npm run dev --prefix apps/gops-frontend
```

Minimum browser viewports:

- desktop wide
- desktop narrow panel
- mobile/narrow width

## M0: Baseline Audit And Safety

### Goal

Confirm the current implementation shape before changing behavior.

### Implementation

- Audit current chart API routes, serving providers, DTOs, chart state, layer keys, pane ratio model, drawing model, and semantic drill-down path.
- Confirm current SMA behavior through `ma5`, `ma20`, `ma60`.
- Confirm current Volume auto-disable behavior and pane height ratio model.
- Confirm Redis/ClickHouse source paths for candles, trades, quotes, and volume profile bins.
- Record findings in implementation notes or commit/PR summary; do not add noisy docs unless needed.

### Static Checks

```sh
git diff --check
rg -n "ChartLayerKey|ma5|ma20|ma60|volumeRatio|heightRatio|footprint|volume-profile-bins" apps systems shared platform infra
```

### Tests

- No new test is required in M0 unless audit reveals a broken baseline.

### Browser Verification

- Capture baseline behavior for:
  - Candle chart
  - Volume pane
  - MA 5/20/60 rendering
  - drawing tools
  - existing `1m -> footprint` placeholder/drill-down path
  - current behavior when chart panel height shrinks and VOL disappears

### Exit Criteria

- Current behavior is understood.
- No unrelated user changes are reverted.
- M1 can proceed with clear baseline facts.

## M1: Layer, Pane, And Chart Type Foundation

### Goal

Create the structural foundation for chart types, layer registry, derived interval, pane sizing, and draggable pane boundaries.

### Implementation

- Separate `chartType` from `interval`.
- Add base chart renderers/states for:
  - Candle
  - Line
  - OHLC Bar
- Add `footprint` as a derived interval after `1m`, without requiring real footprint data yet.
- Preserve existing candle data loading for Candle and OHLC Bar.
- Disable dig interactions for Line chart.
- Map existing `ma5`, `ma20`, `ma60` into SMA preset layer ids:
  - `sma:5`
  - `sma:20`
  - `sma:60`
- Introduce extensible layer metadata with `placement` and `supportedPlacements`.
- Restore pane ratio command/action path.
- Restore draggable dark boundary between base chart and first below pane.
- Preserve pane ratios in the chart document.
- Define concrete `baseChartMinHeight` and `belowPaneMinHeight` from current VOL behavior.

### Static Checks

```sh
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
git diff --check
```

### Tests

- Add or update frontend/chart-engine tests for:
  - chart type state
  - SMA preset mapping
  - footprint derived interval ordering
  - line chart dig disabled
  - pane ratio mutation
  - min-height capacity helpers if introduced

### Browser Verification

- Verify nav dropdown switches Candle, Line, and OHLC Bar.
- Verify Candle and OHLC Bar can open dig charts.
- Verify Line chart does not expose dig interactions.
- Verify `Footprint` appears after `1m` in interval ordering.
- Verify dark boundary drag resizes base/VOL panes.
- Verify pane ratios persist after interaction/state refresh where current app persistence allows.

### Exit Criteria

- Existing candle, volume, MA, drawing, and WebSocket behavior still works.
- Base chart type and pane model are ready for new layers.

## M2: Backend Derived Indicator Data

### Goal

Move preset indicator series to backend-derived data with separated calculation modules and Redis cache.

### Implementation

- Add separated calculation modules for:
  - SMA
  - EMA
  - WMA
  - Bollinger Bands
  - RSI
  - Stochastic
  - MACD
- Add a backend indicator query path, for example `GET /api/charts/indicators`.
- Accept preset layer ids only.
- Read canonical candle series from existing serving/ClickHouse paths with enough lookback for warmup.
- Return timestamp-aligned series with `null` warmup gaps.
- Add Redis cache around responses.
- Use indicator TTL default `300s`, configurable by environment variable.
- Preserve compatibility for existing `ma5`, `ma20`, `ma60`.

### Static Checks

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
git diff --check
```

### Tests

- Unit tests for each indicator formula with deterministic candle fixtures.
- API/service tests for:
  - valid preset layer request
  - multiple layer request
  - warmup `null` handling
  - Redis cache hit/miss path using fakes/mocks
  - existing `ma5/ma20/ma60` compatibility

### Browser Verification

- Toggle SMA/EMA/WMA/Bollinger/RSI/Stochastic/MACD after frontend wiring in M3.
- If M2 is backend-only, verify endpoint responses through API calls and defer visual confirmation to M3.

### Exit Criteria

- Indicator calculation source of truth is backend-derived.
- Calculation logic is not embedded in route handlers.
- Indicator cache uses Redis + ClickHouse/candle source fallback.

## M3: Frontend Indicator Layers And Chart Add Dock

### Goal

Expose new indicator layers through a chart add dock with placement-aware controls.

### Implementation

- Add chart add tool button near chart navigation controls.
- Implement lower chart add dock using the drawing tool dock pattern.
- Add left dock controls:
  - close `X`
  - overlay icon button
  - below-pane icon button
- Move existing MA buttons into the chart add dock as SMA presets.
- Add preset layer buttons for all in-scope indicators.
- Fetch server-derived indicator series and merge by stable layer id.
- Enforce placement compatibility:
  - overlay allows price-compatible layers.
  - overlay disables RSI, Stochastic, MACD.
  - below enables RSI, Stochastic, MACD when height allows.
- Support multiple below panes.
- Enforce capacity using:
  - `baseChartMinHeight + belowPaneCount * belowPaneMinHeight`
- When panel shrinks, remove only excess below panes in LIFO order.
- Do not auto-restore removed panes.
- Preserve drawing layer behavior.

### Static Checks

```sh
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
git diff --check
```

### Tests

- Frontend/chart tests for:
  - chart add dock open/close state
  - placement toggle state
  - disabled buttons in overlay mode
  - enabled oscillator buttons in below mode
  - layer add/remove state
  - below pane capacity and LIFO removal
  - no auto-restore after resize

### Browser Verification

- Verify chart add dock opens from nav/top control.
- Verify overlay/below icon toggle.
- Verify incompatible chart buttons disable in overlay mode.
- Verify RSI/Stochastic/MACD add below when height allows.
- Verify multiple below panes stack.
- Shrink chart panel and confirm below panes disappear one at a time from the bottom/latest-added pane.
- Expand chart panel and confirm removed panes do not auto-restore.
- Verify drawing tools still open and drawings still render.

### Exit Criteria

- Users can add/remove in-scope preset layers from the chart add dock.
- Placement and capacity rules match the Roadmap.
- Existing chart interactions remain intact.

## M4: Volume Profile Backend Buckets

### Goal

Render visible-range Volume Profile using backend-computed readable display buckets.

### Implementation

- Extend `GET /api/charts/volume-profile-bins` with:
  - `targetBins`
  - `priceMin`
  - `priceMax`
- Compute display buckets in a separated backend module.
- Use readable rounded price steps.
- Default target is about 10 buckets, but exact count may be 10-ish.
- Add Redis cache with default TTL `30s`, configurable by environment variable.
- Return enough data for:
  - bucket low/high or representative price
  - volume
  - trade count
  - vwap if available
  - POC
  - Value Area
  - coverage/partial metadata
- Frontend sends current visible time range and visible price range.
- Debounce pan/zoom driven requests and ignore stale responses.
- Render profile histogram on the price pane.
- Render POC and Value Area.

### Static Checks

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
git diff --check
```

### Tests

- Backend tests for:
  - rounded bucket boundaries
  - target bucket count 10-ish behavior
  - POC selection
  - Value Area calculation
  - empty/partial data response
  - Redis cache key and TTL behavior
- Frontend tests for:
  - visible range request params
  - stale response ignored
  - profile render state
  - empty state

### Browser Verification

- Enable Volume Profile.
- Pan and zoom the chart and verify profile updates to visible range.
- Verify readable 10-ish buckets.
- Verify POC and Value Area render.
- Verify no fake profile appears when data is missing.

### Exit Criteria

- Volume Profile is backend bucketed, visible-range aware, cached, and visually verified.

## M5: Footprint 1m Derived Interval

### Goal

Implement 1m-only estimated Footprint as a derived interval and drill-down target.

### Implementation

- Add backend footprint query path.
- Query ClickHouse `trade_ticks` and `quote_ticks`.
- Aggregate into 1m time buckets and price bins only.
- Align trades with nearest quote at or before trade timestamp when feasible.
- Classify side as:
  - estimated ask-side
  - estimated bid-side
  - unknown
- Keep unknown volume visible.
- Add Redis cache with default TTL `15s`, configurable by environment variable.
- Add `Footprint` after `1m` in interval dropdown.
- Connect existing `1m -> footprint` drill-down path to the same data.
- Render each 1m bucket as a footprint column.
- Show `Estimated` label inside chart at top-right, matching the existing top-left OHLC hover readout style.
- Support missing/partial states.

### Static Checks

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
git diff --check
```

### Tests

- Backend tests for:
  - 1m bucketing
  - price bin aggregation
  - quote alignment
  - estimated ask/bid/unknown classification
  - Redis cache key and TTL behavior
  - no generic multi-interval footprint path
- Frontend tests for:
  - interval dropdown ordering
  - drill-down to footprint
  - Estimated label render
  - partial/missing state

### Browser Verification

- Select `Footprint` from interval dropdown.
- From `1m`, open dig chart and confirm it shows Footprint.
- Verify Footprint always uses 1m source buckets.
- Verify `Estimated` appears at top-right inside chart.
- Verify unknown/partial state is visible and non-fabricated.

### Exit Criteria

- Footprint is usable from interval dropdown and `1m` drill-down.
- It is clearly labeled estimated.
- It is optimized for 1m source buckets only.

## M6: Final Integration And Regression

### Goal

Verify the full chart enhancement works as one coherent experience and does not regress existing behavior.

### Implementation

- Confirm chart state persistence covers chart type, layer visibility, layer params, placement, pane ratios, and footprint interval state.
- Confirm WebSocket candle updates still update base charts.
- Confirm drawing tools still work with the drawing layer.
- Confirm existing `/api/charts/candles` consumers remain compatible.
- Confirm missing data states never fabricate market data.

### Static Checks

Run the full verification set:

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/market-data/tests
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
docker compose config --quiet
git diff --check
```

### Tests

- Ensure all milestone tests remain passing.
- Add regression tests for any bug found during browser verification.

### Browser Verification

Verify all required flows on desktop wide, desktop narrow panel, and mobile/narrow width:

- Candle/Line/OHLC chart type dropdown
- Candle/OHLC dig enabled, Line dig disabled
- Chart add dock
- Overlay/below placement disabled states
- Draggable pane boundary
- Multiple below panes
- LIFO below pane removal
- No auto-restore after height grows
- Backend-derived indicators
- Volume Profile visible-range buckets, POC, Value Area
- Footprint interval, drill-down, Estimated label
- Drawing tools regression
- WebSocket/live candle update behavior if local runtime supports it

### Exit Criteria

- Full verification set passes or any skipped command is explicitly documented with reason.
- Browser verification passes across required viewports.
- No unrelated user changes are reverted.
- Goal implementation can be summarized with completed milestone evidence.
