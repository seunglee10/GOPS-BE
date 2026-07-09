# 04 — Frontend

All paths under `apps/gops-frontend/` unless prefixed `chart-engine/` (= `apps/chart-engine/`).
Reminder: the live renderer is `src/chart/ChartCanvas.tsx`; chart-engine's `canvasRenderer.ts` is
unused by the app and must NOT be extended (leave it alone).

## 1. Chart type `bidask` — type plumbing (4 places, 2 packages)

1. `src/chart/types.ts`: `export type ChartType = "candle" | "line" | "ohlc" | "bidask";` and add
   `"bidask"` to `chartTypes`.
2. `chart-engine/src/types.ts`: same union extension.
3. `chart-engine/src/commands.ts`: the local `const chartTypes: ChartType[] = [...]` validation
   list (used by `readChartType` to validate `chart.type.set`) must include `"bidask"`, otherwise
   the command is silently rejected.
4. `src/chart/chartDocumentAdapter.ts`: `normalizeFrontendChartType` must accept `"bidask"`.

Dropdown label: find where chart-type labels are produced for the `<select>` in
`src/components/PanelContentRenderer.tsx` (`chartTypeLabel` or inline) and label `bidask` as
`Bid/Ask`.

## 2. Shared domain module — `src/chart/orderFlow.ts` (types + pure transforms)

```ts
export type OrderFlowLevelDto = {
  priceBin: number; askVolume: number; bidVolume: number; unknownVolume: number;
  askTradeCount?: number; bidTradeCount?: number; unknownTradeCount?: number;
};
export type OrderFlowDayDto = {
  sessionDate: string;
  totals: { askVolume: number; bidVolume: number; unknownVolume: number; delta: number;
            tradeCount: number; volume: number };
  levels: OrderFlowLevelDto[];
};
export type OrderFlowDailyResponseDto = {
  symbol: string; priceBinSize: number; sideClassification: "estimated";
  classificationVersion: string; from: string; to: string;
  dataStatus: "ready" | "empty" | "unsupported"; days: OrderFlowDayDto[];
  supportedSymbols?: string[];
};
export type OrderFlowMinuteDto = { eventMinute: string; bins: OrderFlowLevelDto[] };
export type OrderFlowIntradayResponseDto = {
  symbol: string; sessionDate: string; priceBinSize: number;
  dataStatus: "ready" | "empty" | "unsupported"; minutes: OrderFlowMinuteDto[];
  liveQuote: { bidPrice?: number; askPrice?: number; bidSize?: number; askSize?: number;
               timestamp?: string } | null;
  supportedSymbols?: string[];
};

// ---- ladder: the single normalized renderer input (handoff D4) ----
export type OrderFlowLadderLevel = OrderFlowLevelDto & {
  delta: number; totalVolume: number;
  askImbalance: boolean; bidImbalance: boolean;
};
export type OrderFlowLadder = {
  priceStep: number;
  levels: OrderFlowLadderLevel[];        // descending price
  minPrice: number; maxPrice: number;
  pocPriceBin: number | null;            // highest totalVolume level
  totals: OrderFlowDayDto["totals"];
  maxLevelVolume: number;                // for bar-width scaling
  label?: string;                        // e.g. session date or window description
};

export function rebinLevels(levels: OrderFlowLevelDto[], fromStep: number, toStep: number): OrderFlowLevelDto[];
export function sumMinuteWindows(minutes: OrderFlowMinuteDto[], windowMinutes: number | "session"): OrderFlowLevelDto[];
export function buildLadder(levels: OrderFlowLevelDto[], priceStep: number, label?: string): OrderFlowLadder;
export const ORDER_FLOW_IMBALANCE_RATIO = 3.0;
export const ORDER_FLOW_IMBALANCE_MIN_SHARE = 0.05; // of maxLevelVolume
export const ORDER_FLOW_PRICE_STEPS = [0.01, 0.05, 0.1, 0.25, 0.5, 1] as const;
export const ORDER_FLOW_WINDOWS = ["1m", "10m", "1h", "session"] as const;
```

Transform rules:

- `rebinLevels`: map each level to `round(round(priceBin / toStep) * toStep, 6)` and sum fields.
  `toStep` must be ≥ `fromStep`; assert multiples.
- `sumMinuteWindows`: `"session"` = all minutes; numeric = the trailing N minutes by `eventMinute`
  ordering (not wall clock — the latest N distinct minutes present).
- `buildLadder`: sort descending by priceBin, fill `delta`/`totalVolume`, POC = max totalVolume
  (ties → the higher-volume-then-lower-price rule; pick one and test it). **Imbalance (diagonal,
  standard convention):** level `p` is `askImbalance` when
  `askVolume(p) >= ORDER_FLOW_IMBALANCE_RATIO * bidVolume(p - priceStep)` and
  `totalVolume(p) >= ORDER_FLOW_IMBALANCE_MIN_SHARE * maxLevelVolume` (treat missing/0 opposing
  volume with `askVolume(p) > 0` as imbalance only when the volume floor passes); `bidImbalance`
  mirrored: `bidVolume(p) >= ratio * askVolume(p + priceStep)`.

These utils are data-source-agnostic — daily columns, intraday windows, and (Phase 2) the VP
overlay can all use them.

## 3. Shared renderer — `src/chart/orderFlowRender.ts`

One pure canvas function both views call; it must know nothing about data origin:

```ts
export type OrderFlowLadderRect = { x: number; y: number; width: number; height: number };
export type OrderFlowRenderOptions = {
  showLevelText: boolean;        // bid/ask numbers per row (only when rows are tall enough)
  highlightImbalance: boolean;
  showPocLine: boolean;
  showDeltaFooter: boolean;      // total delta label under the column
  priceToY?: (price: number) => number;  // when embedding in ChartCanvas price space
};
export function drawOrderFlowLadder(
  ctx: CanvasRenderingContext2D,
  rect: OrderFlowLadderRect,
  ladder: OrderFlowLadder,
  theme: ReturnType<typeof readThemeColors>,
  options: OrderFlowRenderOptions,
): void;
```

Visual spec (reuse the palette conventions from the removed footprint drawing — read the current
`drawFootprintBucket` before deleting it and keep its good parts):

- Each level row: bid bar leftward from the column center (`colors.downSoft`), ask bar rightward
  (`colors.upSoft`), width ∝ `value / ladder.maxLevelVolume`; unknown drawn as a thin centered
  neutral bar (`colors.axis`) only when `showLevelText` (declutter).
- Row fill tint by level delta sign/magnitude (alpha ≤ ~0.6 like `footprintLevelAlpha`).
- POC: horizontal accent line + slightly brighter row background at `pocPriceBin`.
- Imbalance: outline (1px) on the imbalanced side's bar in the up/down accent color.
- `showDeltaFooter`: `Δ {formatted totals.delta}` centered under the column, colored by sign.
- When `priceToY` is provided, rows are positioned by price (chart embedding); otherwise rows fill
  `rect` evenly (panel standalone mode).
- Min row height 2px; when row height < 9px, suppress text regardless of `showLevelText`.

Also export `drawEstimatedBadge(ctx, x, y, theme)` — a small `estimated` pill; both views draw it
(low-key emphasis: axis color, 10px font — the delegated "estimated" prominence decision).

## 4. Data client — `src/chart/orderFlowClient.ts`

Follow `cdcClient.ts` conventions (plain `fetch`, `ChartApiError`, `signal?` param):

```ts
export async function fetchOrderFlowSymbols(signal?: AbortSignal): Promise<{ symbols: string[]; priceBinSize: number }>;
export async function fetchOrderFlowDaily(q: { symbol: string; from: string; to: string; limitDays?: number }, signal?: AbortSignal): Promise<OrderFlowDailyResponseDto>;
export async function fetchOrderFlowIntraday(symbol: string, signal?: AbortSignal): Promise<OrderFlowIntradayResponseDto>;
```

Module-level cache for `fetchOrderFlowSymbols` (session-static). WS: reuse `openChartSocket` from
`cdcClient.ts`; add the event pass-through so `ORDER_FLOW_BINS_UPDATE` reaches the `onEvent`
callback — check `normalizeCandleEvent`/`isRealtimeLayerEventDto` in `cdcClient.ts`/`types.ts`:
extend the realtime-layer union with

```ts
{ type: "ORDER_FLOW_BINS_UPDATE"; symbol: string; interval?: string; data: OrderFlowMinuteUpdate }
// OrderFlowMinuteUpdate = { eventMinute: string; sessionDate: string; priceBinSize: number;
//                           bins: OrderFlowLevelDto[]; updatedAt: string }
```

and let it pass validation (it carries `interval: "1m"`, so the existing interval-required check
passes; verify). **Do not** route it into the chart-engine runtime reducer (`chart.layer.live`
handles only trades/quotes) — both consumers handle it locally (§5, §6).

## 5. View A — main chart panel, chartType `bidask`

### 5.1 Interval lock — `src/components/PanelContentRenderer.tsx`

- When current `chartType === "bidask"`: render the interval `<select>` disabled with value `1D`.
- On switching chart type **to** `bidask`: if interval ≠ `1D`, dispatch `setInterval("1D")` via the
  existing `chartPanelHandleRef.current.setInterval` before/with `setChartType("bidask")`.
- On switching away: leave interval at `1D` (user changes it manually; no magic restore).
- Non-pinned symbol: the `bidask` option stays selectable (the chart itself shows the unsupported
  empty state, §8) — simpler than option-gating and keeps the dropdown static.

### 5.2 Data — `src/components/ChartPanel.tsx`

Replace the deleted footprint effect (`05` §2) with an order-flow effect, active when
`chart.chartType === "bidask" && chart.interval === "1D"`:

- Local state `const [orderFlowDaily, setOrderFlowDaily] = useState<OrderFlowDailyResponseDto | null>(null)`
  and `const [orderFlowToday, setOrderFlowToday] = useState<Map<string, OrderFlowMinuteDto>>(new Map())`
  (minute-keyed).
- Fetch `fetchOrderFlowDaily` for the **visible date range** (derive from the same
  visible-range computation the old footprint effect used — `visibleProfileRange` — mapped to
  session dates), refetch on symbol/viewport-range change, abort-on-cleanup.
- Fetch `fetchOrderFlowIntraday` once on activation to seed today, then apply
  `ORDER_FLOW_BINS_UPDATE` events (the panel's existing `openChartSocket` `onEvent` handler
  receives them because delivery is interval-agnostic; add a branch before the runtime dispatch:
  `if (event.type === "ORDER_FLOW_BINS_UPDATE") { setOrderFlowToday(replace minute); return; }`).
  **Verify** that ChartPanel opens its socket for `1D` (`isRealtimeStreamInterval("1D")` — live 1D
  candles exist, so it should be true); if it is not, force the socket open when
  `chartType === "bidask"`.
- Merge into `renderChart` (the memo that already merges `indicatorSeries`, `volumeProfile`, …):
  `orderFlow: { daily: orderFlowDaily, today: ladderizedToday }`. Extend `ChartState` in
  `src/chart/types.ts` with `orderFlow?: { daily: OrderFlowDailyResponseDto | null; today: OrderFlowDayDto | null } | null`
  (`today` = `buildLadder`-ready day built from `sumMinuteWindows(minutes, "session")` with
  `sessionDate` = today).
- 1D candles keep loading exactly as for candle mode (the scene, axes, hit-testing, and selection
  all come from candles — `bidask` only changes the base price layer drawing).

### 5.3 Rendering — `src/chart/ChartCanvas.tsx`

In `drawBasePriceLayer`, replace the removed `interval === "footprint"` branch with:

```ts
if (scene.chart.chartType === "bidask") { drawOrderFlowColumns(context, scene); return; }
```

`drawOrderFlowColumns(context, scene)` (new function in ChartCanvas.tsx):

- Build a `Map<sessionDate, OrderFlowDayDto>` from `scene.chart.orderFlow.daily.days` plus
  `orderFlow.today` (today's entry wins).
- Choose a **display price step** automatically from the candle width / visible price range so
  each ladder has ~20–60 rows: pick the smallest step in `ORDER_FLOW_PRICE_STEPS` where
  `(scene.scales.maxPrice - scene.scales.minPrice) / step <= 60`. (Coarse daily view per handoff;
  no user knob in View A for MVP.)
- For each visible 1D candle unit (`scene` semantic/candle units): map its timestamp to session
  date; if a day exists → `buildLadder(rebinLevels(day.levels, 0.01, step), step)` (memoize per
  day+step outside the rAF loop — module-level WeakMap/LRU keyed on the day object + step) →
  `drawOrderFlowLadder(ctx, unitRect, ladder, theme, { showLevelText: false, highlightImbalance: true,
  showPocLine: true, showDeltaFooter: true, priceToY: (p) => priceToY(scene, p) })`.
- Day missing → draw the ghost-candle fallback (port `drawFootprintGhostCandle` before deleting).
- Empty/unsupported → §8 empty state.
- Draw `drawEstimatedBadge` once per chart (top-left of plot, where the footprint estimated label
  sat).

### 5.4 Column click-select + agent reference

Candles still back the scene, so `hitTestSemanticNode` + `toggleAgentSemanticUnitSelection` work
unchanged (`chartType !== "line"` gate already passes for `bidask`; unit kind is `"candle"`). Two
extensions:

1. `src/agent/agentReferences.ts`:
   - `AgentReferenceType` += `"chart.orderFlow"`.
   - New builder:

   ```ts
   export function chartOrderFlowReference(
     selection: SemanticSelectionSnapshot,
     day: { sessionDate: string; totals: OrderFlowDayDto["totals"]; pocPriceBin: number | null } | null,
     sourcePanelId?: string,
   ): AgentReference {
     return {
       type: "chart.orderFlow", sourcePanelId,
       displayLabel: `${selection.symbol} ${day?.sessionDate ?? selection.from} Order Flow`,
       data: { ...selection, orderFlow: day ?? undefined, sideClassification: "estimated" },
     };
   }
   ```

   - `agentReferenceChipKind` needs no change (non-`news` → `"candle"` chip is acceptable for MVP).
2. Where the semantic selection becomes a reference (`buildChartAnalysisContext` and the
   `SEMANTIC_SELECTION_REFERENCE_KEY` chip path in `App.tsx`): when the source chart's
   `chartType === "bidask"`, use `chartOrderFlowReference(selection, daySummaryFor(selection))`
   instead of `chartCandleReference(selection)`. `daySummaryFor` looks up the selected date in the
   chart's `orderFlow` state (`totals` + POC via `buildLadder` memo). Plumb the chart type through
   however `buildChartAnalysisContext` currently receives `chart` (it already gets `ChartState`).

## 6. View B — intraday tile panel

### 6.1 Registration

- `src/layout/panelLayout.ts`: `PanelContentKind` += `"orderFlow"` (insertable list derives from
  the registry automatically).
- `src/layout/agentLayoutTypes.ts`: `AgentLayoutPanelType` += `"orderFlowProfile"`; update any
  `kindToPanelType`/`panelTypeToKind` maps (grep for them — `panelRegistry.ts` provides
  `panelKindForAgentType` generically, but check `docs/about_front.md`'s list for extra maps).
- `src/layout/panelRegistry.ts`: append

  ```ts
  { kind: "orderFlow", title: "오더플로우", agentPanelType: "orderFlowProfile",
    minSpan: { colSpan: 1, rowSpan: 1 }, defaultSpan: { colSpan: 1, rowSpan: 2 },
    defaultLayoutWeight: 45 }
  ```

- `src/components/PanelContentRenderer.tsx`: add the `content.kind === "orderFlow"` branch
  rendering `<OrderFlowPanel …/>`; pass `panelSymbol` (see 6.2) and `semanticSelection` (see 6.4).

### 6.2 Component — `src/components/OrderFlowPanel.tsx` (new)

Props:

```ts
type OrderFlowPanelProps = {
  panelId: string;
  symbol: string;                       // resolved: content.props.symbol ?? global active symbol
  onSymbolChange?: (symbol: string) => void; // persists to content.props.symbol
  semanticSelection: SemanticSelectionSnapshot | null;  // App-level selection (6.4)
};
```

Per-panel symbol: study how an existing panel persists per-panel props (`PanelContentInstance.props`
in `panelLayout.ts`; `readContentSymbol` in `PanelWorkspace.tsx` shows the read path;
`onChangePanelChartSymbol` shows a write path for chart panels). Wire an equivalent props-update
for the orderFlow panel so its chosen symbol survives layout persistence. **If no generic
user-driven props-update path exists** (likely — chart panels have a bespoke one), add one:
a callback threaded from `App.tsx` (owner of the `TiledPanelState` `useState`) →
`PanelWorkspace` → `PanelContentRenderer` that does
`setPanelState(s => ({ ...s, contents: { ...s.contents, [contentId]: { ...c, props: { ...c.props, symbol } } } }))`
— mirroring how `onChangePanelChartSymbol` is threaded. Header UI: a `<select>` of
`fetchOrderFlowSymbols().symbols` (pin-only list) + the panel title `Order Flow Profile`.

State machine:

```
mode: "live" | "selected"
live:     snapshot = fetchOrderFlowIntraday(symbol)
          socket   = openChartSocket(symbol, "1m", onEvent, onState)
            - ORDER_FLOW_BINS_UPDATE → minutes.set(eventMinute, bins)   (full replace)
            - LIVE_QUOTE_UPDATE      → liveQuote = data                  (L1 display)
            - everything else ignored
          window slider: ORDER_FLOW_WINDOWS ("1m"|"10m"|"1h"|"session"), default "10m"
          priceStep select: ORDER_FLOW_PRICE_STEPS, default 0.05
          ladder = buildLadder(rebinLevels(sumMinuteWindows(minutes, window), 0.01, step), step)
          if snapshot.dataStatus === "empty" (market closed / no data yet):
            fetch latest completed session via fetchOrderFlowDaily(symbol, last 7 days, limitDays 1)
            → render that day, slider disabled at "session", badge "last session {date}"
selected: (entered per 6.4) fetchOrderFlowDaily(symbol, from=to=selectedDate)
          slider locked to "day"; socket stays open but events only update the (hidden) live cache
          banner: "{date} · daily aggregate" (returns to live on deselect)
```

Rendering: a `<canvas>` sized to the panel body; each frame (or on state change — no rAF loop
needed; redraw on data/props change) call `drawOrderFlowLadder` with `rect` = full body minus
header/footer, `showLevelText: true`, `highlightImbalance: true`, `showPocLine: true`,
`showDeltaFooter: true`, no `priceToY` (even rows). Beside the ladder (right edge), when `liveQuote`
present in live mode: bid/ask prices + sizes at their price rows (or a compact top-right readout if
row-alignment is fiddly — implementer's choice, keep it legible). Header row: symbol select, mode
badge, window slider, price-step select, total delta readout, `estimated` badge
(`drawEstimatedBadge` or a DOM pill — DOM is fine here).

Cleanup: abort fetches, close socket on unmount/symbol change. Reuse `styles.css` global classes;
add new `order-flow-panel*` classes there (no CSS modules in this repo).

### 6.3 Non-pinned symbol

If resolved `symbol` is not in `fetchOrderFlowSymbols().symbols`: render the §8 unsupported state
(with the symbol select still usable to switch to a pinned one). Default panel symbol on first
insert: `NVDA` if the global active symbol isn't pinned.

### 6.4 Click linking (user-confirmed rule — implement exactly)

`App.tsx` already owns `semanticSelection` (the chart click selection). Thread it into
`PanelContentRenderer` → `OrderFlowPanel` (some plumbing exists for
`emphasizeChartSelection`; follow that path).

In `OrderFlowPanel`:

```ts
const linked = semanticSelection
  && semanticSelection.symbol === symbol          // symbols must match — the ONLY link condition
  && semanticSelection.interval === "1D";         // day-unit selections only
useEffect(() => { setMode(linked ? "selected" : "live");
                  if (linked) setSelectedDate(sessionDateOf(semanticSelection)); }, [linked, ...]);
```

`sessionDateOf(selection)`: ET date of `selection.timestamp ?? selection.from`. Works identically
whether the source chart shows candles/OHLC or `bidask` (both produce 1D semantic selections).
Deselection (`semanticSelection` null or symbol/interval mismatch) → back to `live` mode
automatically. No other coupling between panels.

## 7. WS/live-quote note

The intraday panel gets `LIVE_QUOTE_UPDATE` on its own socket (symbol-matched, interval-agnostic —
existing behavior), so it does **not** need the chart-runtime `liveQuotesBySymbol` store. Do not
add cross-panel store coupling.

## 8. Unsupported / empty states (pin-only MVP)

One shared presentational helper (DOM for the panel, canvas text for the chart — mirror the old
`drawFootprintEmptyState`):

- Unsupported symbol: `Order Flow는 아직 {SYMBOL}을 지원하지 않아요 · 지원: NVDA, AMZN, MU, AAPL, GOOGL`
  (list from `fetchOrderFlowSymbols`, don't hardcode).
- Supported but no data (`dataStatus:"empty"`): `아직 수집된 오더플로우 데이터가 없어요` (+ date
  where applicable).

## 9. Existing VP overlay (preserve; optional shared-util adoption)

The `volume-profile` layer, `fetchVolumeProfile`, and its ChartCanvas drawing stay functionally
untouched. Allowed only if zero-behavior-change: reusing `buildLadder`'s POC/binning helpers
internally. If any doubt, skip — preservation outranks dedup. Leave a code comment near the VP
overlay renderer: `Phase 2 candidate: adopt tick-based order-flow bins when symbol coverage allows
(see bid_ask_vp_plan/00 §Deviations)`.

## 10. Tests (frontend)

Extend the custom runner suite (`tests/chartRuntime.test.ts` or a sibling `tests/orderFlow.test.ts`
wired into `scripts/run-chart-tests.mjs`):

- `chart.type.set` with `"bidask"` passes chart-engine command validation; document adapter
  normalizes it; legacy persisted interval `"footprint"` migrates (see `05` §4).
- `rebinLevels` (0.01→0.25 sums correctly, grid alignment), `sumMinuteWindows` (trailing-N and
  session), `buildLadder` (POC tie rule, delta, ask/bid imbalance including missing-diagonal
  cases), all with hand-computed fixtures.
- ORDER_FLOW_BINS_UPDATE minute-replace semantics: applying events out of order / duplicated leaves
  the same map as applying only the last per minute.
