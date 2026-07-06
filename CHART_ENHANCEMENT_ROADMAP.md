# Chart Enhancement Roadmap

이 문서는 GOPS 차트 고도화의 제품/기술 결정 source of truth다.
Codex Goal 모드에서 구현할 때는 이 문서와 [CHART_ENHANCEMENT_MILESTONE.md](./CHART_ENHANCEMENT_MILESTONE.md)를 함께 참조한다.

- Roadmap: 무엇을 만들고 어떤 정책을 지킬지 정의한다.
- Milestone: 어떤 순서로 구현하고 어떻게 검증할지 정의한다.

## Goal

현재 candle 중심 차트를 주식 차트 서비스에 필요한 다중 차트/지표 구조로 확장한다.

구현 목표:

- 기본 가격 차트를 Candle, Line, OHLC Bar 중 선택할 수 있게 한다.
- 가격 overlay, below pane, footprint mode를 layer로 관리한다.
- 차트 지표와 구조 데이터를 backend-derived data로 제공한다.
- 렌더링은 CSR을 유지하되, 계산 결과는 서버가 안정적으로 공급한다.
- 이후 AI Agent가 같은 차트 데이터와 layer metadata를 참조할 수 있게 구조를 열어둔다.

## Scope

In scope:

- Base chart type selector
  - Candle
  - Line
  - OHLC Bar
- Preset indicators
  - SMA 5/20/60
  - EMA 20
  - WMA 20
  - Bollinger Bands 20, 2
  - RSI 14
  - Stochastic 14, 3, 3
  - MACD 12, 26, 9
- Structure layers
  - Volume
  - Volume Profile
  - POC / Value Area
  - Footprint
- Chart add tool dock
- Overlay/below layer placement
- Multi-below-pane layout and resizing
- Drawing result layer management

Out of scope:

- AI Agent automatic chart selection, interpretation, or drawing generation
- Custom indicator parameter editing UI
- VWAP, Anchored VWAP, ATR, RVOL
- Bollinger Bandwidth, Supertrend, Donchian Channel, Keltner Channel, Historical Volatility
- Market Status, LULD, Halt/Resume, Correction, News/Event markers
- Background cache warmer or always-on derived chart worker
- Broad new ClickHouse materialized tables unless a later bottleneck proves they are needed

## Current Baseline

Goal implementation must verify these facts before editing code:

- `GET /api/charts/candles` is the current base candle API and must remain compatible.
- `GET /api/charts/volume-profile-bins` already exists and should be extended, not replaced.
- Current frontend `CandleDto` is based on OHLCV plus `ma5`, `ma20`, `ma60`.
- Current layer keys are fixed around `candles`, `volume`, `ma5`, `ma20`, `ma60`.
- Current SMA-like behavior already exists as `ma5`, `ma20`, `ma60`; it should be mapped into `sma:5`, `sma:20`, `sma:60` preset layers.
- `ChartDocument.panes` already stores pane `heightRatio`; pane resizing should reuse or generalize this model.
- Previous UX allowed dragging the dark boundary between base chart and VOL pane; this must be restored.
- Current semantic timeline has a `1m -> footprint` placeholder path; this must become real 1m estimated footprint data.
- Durable source tables include `chart_candles`, `trade_ticks`, `quote_ticks`, and `volume_profile_bins_1m`.

## Product Contract

### Base Chart Types

`chartType` and `interval` are separate concepts.

- `chartType` controls renderer:
  - `candle`
  - `line`
  - `ohlc`
- `interval` controls source granularity or derived view.
- `footprint` is added after `1m` in the interval dropdown and maps to 1m source data.

Dig support:

- Candle: supported
- OHLC Bar: supported
- Line: unsupported

### Layer Model

Move from fixed layer keys toward a layer registry.

Each visible layer has:

- stable `id`
- `kind`
- `label`
- `paneId`
- `source`
- `params`
- `style`
- `visible`
- `placement`
- `supportedPlacements`
- optional `metadata`

Required categories:

- base price layer
- price overlay
- volume pane
- oscillator pane
- structure overlay
- footprint mode
- drawing layer

Canonical layer ids:

- `base-price:candle`
- `base-price:line`
- `base-price:ohlc`
- `sma:5`
- `sma:20`
- `sma:60`
- `ema:20`
- `wma:20`
- `bollinger:20:2`
- `rsi:14`
- `stochastic:14:3:3`
- `macd:12:26:9`
- `volume`
- `volume-profile:visible-range`
- `footprint:estimated`
- `drawings:user`

### Placement Rules

Placements:

- `overlay`: draw on the main chart area with the same x-axis.
- `below`: draw in a separate pane below the base chart, sharing the same x-axis.

Rules:

- Price-compatible layers can be added in overlay mode.
- Non-price-scale layers are disabled in overlay mode.
- RSI, Stochastic, and MACD are addable only in below mode.
- If below placement is unavailable, the below button is disabled and placement switches to overlay.
- Existing drawing entities are managed as one drawing layer first; per-drawing layer ordering is future work.

### Pane Layout And Resizing

Below panes can stack when chart panel height is sufficient.

Minimum height policy:

- `baseChartMinHeight` is derived from the current base chart height when the chart panel is at its minimum height.
- `belowPaneMinHeight` is derived from the current VOL pane height immediately before VOL disappears.
- A new below pane can be added only when:
  - `panelHeight >= baseChartMinHeight + (activeBelowPaneCount + 1) * belowPaneMinHeight`

Shrink behavior:

- When the panel shrinks, reduce below pane heights first.
- After below panes reach minimum height, reduce base chart height.
- If the panel can no longer satisfy `baseChartMinHeight + activeBelowPaneCount * belowPaneMinHeight`, remove below panes in LIFO order.
- Remove only enough below panes to satisfy the minimum-height formula.
- Do not remove all below panes at once unless no below pane can fit.
- Removed panes are not automatically restored when height grows again.

Pane boundary resizing:

- Restore the draggable dark boundary between base chart and the first below pane.
- The boundary is visually the base chart floor line when a below pane exists.
- Dragging resizes adjacent panes while respecting `baseChartMinHeight` and `belowPaneMinHeight`.
- Dragging never removes panes; removal is only for panel-height capacity enforcement.
- Persist adjusted pane ratios in `ChartDocument.panes[].heightRatio` or its generalized successor.
- Restore the command/action path for pane ratio updates; a no-op `setVolumeRatio` path is not sufficient.

### Chart Add Tool

The chart add tool uses the same lower-dock pattern as the drawing tool.

Dock requirements:

- A nav/top icon opens the chart add dock.
- Left controls include:
  - close `X`
  - overlay icon button
  - below-pane icon button
- Overlay/below buttons behave as an exclusive two-button toggle.
- Buttons use icons, not text-only labels.
- Existing MA controls move into this dock as SMA preset buttons.
- Pressing a chart/indicator button toggles that layer on/off.
- Buttons incompatible with the selected placement are disabled.

## Backend-Derived Data Policy

Rendering remains CSR, but derived chart data is prepared by backend services.

Principles:

- API routes do not contain indicator formulas directly.
- Indicator, Volume Profile, and Footprint calculations live in separated calculation modules.
- Backend query services call those modules.
- On cache miss, backend reads ClickHouse/source data, computes, and writes Redis cache.
- This Goal implements on-demand calculation plus Redis cache only.
- No background cache warmer or always-on derived worker in this Goal.
- Future workers should reuse the same calculation modules.

Cache defaults:

- Indicator series TTL: `300s`
- Volume Profile display bucket TTL: `30s`
- Footprint 1m bucket TTL: `15s`
- TTLs must be configurable by environment variables.

### Derived Indicators

Add a backend query path for preset indicator series.

Candidate route:

- `GET /api/charts/indicators`

Params:

- `symbol`
- `interval`
- `from`
- `to`
- `layers`

Preset layer ids:

- `sma:5`
- `sma:20`
- `sma:60`
- `ema:20`
- `wma:20`
- `bollinger:20:2`
- `rsi:14`
- `stochastic:14:3:3`
- `macd:12:26:9`

Response requirements:

- timestamp-aligned series per requested layer
- `null` values for warmup gaps
- source interval metadata
- lookback metadata
- coverage metadata
- calculation version

Existing `ma5`, `ma20`, `ma60` fields must remain compatible.

### Volume Profile

Extend `GET /api/charts/volume-profile-bins`.

Additional optional params:

- `targetBins`
- `priceMin`
- `priceMax`

Behavior:

- Frontend sends current visible time range and visible price range.
- Backend aggregates into display buckets.
- Bucket boundaries use readable rounded price steps.
- Default target is about 10 buckets, but exact bucket count may be 10-ish for readable labels.
- Backend computes POC and Value Area metadata or returns enough data for deterministic rendering.
- Missing/partial data must be explicit; never fabricate profile data.

### Footprint

Footprint is a 1m-only derived microstructure view.

API:

- Add a footprint query path, for example `GET /api/charts/footprint`.

Params:

- `symbol`
- `from`
- `to`
- `priceBinSize`

Rules:

- Always use 1m source buckets.
- Do not build generic multi-interval footprint aggregation.
- Query ClickHouse `trade_ticks` and `quote_ticks`.
- Align each trade with the nearest quote at or before trade timestamp when feasible.
- Classify side as estimated ask-side, estimated bid-side, or unknown.
- Unknown volume remains visible.
- Response metadata includes `sideClassification = "estimated"` and classification version.
- Chart label text is exactly `Estimated`.
- The label appears inside the chart at top-right, matching the style of the existing top-left OHLC hover readout.

## Calculation Modules

Required separated modules:

- moving averages: SMA, EMA, WMA
- Bollinger Bands
- MACD
- RSI
- Stochastic
- Volume Profile display bucket aggregation
- 1m Footprint aggregation and side classification

Module requirements:

- mostly pure functions where practical
- deterministic unit tests
- calculation version string for cache invalidation where useful
- shared by on-demand API services and future workers

## Verification Contract

Every milestone in [CHART_ENHANCEMENT_MILESTONE.md](./CHART_ENHANCEMENT_MILESTONE.md) must include:

- static checks
- unit/API tests where relevant
- frontend chart tests/build where relevant
- browser verification for user-visible chart changes

Baseline full verification commands:

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/market-data/tests
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
docker compose config --quiet
git diff --check
```

## Definition Of Done

The Goal is complete when:

- Candle, Line, and OHLC Bar can be selected from a nav dropdown.
- Candle and OHLC Bar support dig charts.
- Line chart does not expose dig interactions.
- Footprint appears after `1m` in the interval dropdown.
- Footprint always renders from 1m source buckets.
- SMA, EMA, WMA, Bollinger Bands, RSI, Stochastic, and MACD are served by backend-derived indicator data.
- Existing `ma5`, `ma20`, `ma60` behavior is preserved through SMA preset layers.
- Chart add dock toggles all in-scope preset layers.
- Overlay/below placement rules are enforced.
- Multiple below panes stack when height allows.
- Below pane capacity uses the minimum-height formula.
- Below panes are removed one at a time in LIFO order when height shrinks.
- Removed below panes are not auto-restored.
- The dark base/below boundary is draggable again.
- Pane height ratios persist in the chart document.
- Volume Profile renders visible-range rounded buckets from backend parameters.
- POC and Value Area render with Volume Profile.
- Footprint renders from real trades/quotes where available.
- Footprint is labeled `Estimated` at top-right.
- Missing data states do not fabricate market data.
- Existing `/api/charts/candles` behavior remains compatible.
- Existing chart drawing tools and WebSocket candle updates still work.
- Backend chart-derived calculations are covered by deterministic tests.
- The app builds and browser verification passes across desktop, narrow, and mobile-like viewports.
