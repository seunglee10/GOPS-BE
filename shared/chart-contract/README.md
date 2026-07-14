# GOPS Shared Chart Contract

Shared chart-command and chart-analysis-asset contracts for frontend runtime and backend/agent code.

`chart-analysis-asset.schema.json` points to the semantic `geometry` asset stored
per `(symbol, interval)`. Builders persist complete `DrawingEntity` objects and
the frontend applies them without a second compiler. Geometry assets support
`1m`, `5m`, `10m`, `1h`, `4h`, `1D`, `1W`; the general chart can still expose
other intervals independently. The geometry payload stores active `patterns[]`,
one `primaryPattern`, and compatibility `primaryTriangle` fields. It permits at
most eight drawings so a pole plus two boundaries can coexist with level lines.
The optional-compatible `tradePlan` field stores a deterministic, non-executable
pattern scenario. New builders always emit it; older rows may omit it until rebuilt.
Confirmed patterns may include additive `confirmation` timing, boundary, ATR
penetration, and relative-volume evidence. `chart-semantics.ko.json` is the canonical
Korean label catalog for pattern states, actions, and reason codes.
The frontend derives an ephemeral `flagMarker` and, for new-position candidates,
an ephemeral `riskRewardBox` from that plan without consuming the eight persisted
geometry drawing slots.

Chart data storage and transport semantics are defined by
`docs/CHART_DATA_ARCHITECTURE.md`. This contract covers UI/chart command shape;
it must not reintroduce preset-universe preload, fake candle rendering, or direct
frontend access to Redis, S3, or ClickHouse.

Current mirrors:

```text
apps/chart-engine/src/types.ts
apps/chart-engine/src/capabilities.ts
systems/api-server/pods/api-server/gops-backend/app/contracts/chart.py
```

Rules:

- LLM agents return `ChartProposal`; they do not mutate `ChartDocument` directly.
- UI and agents use the same `ChartCommand` vocabulary.
- Command payloads must be JSON-serializable.
- Invalid commands must not change chart state.
- Canonical intervals are `1m`, `5m`, `10m`, `1h`, `4h`, `1D`, `1W`, `1M`.
- Candle readiness uses both `dataStatus` and detailed `coverage`.
- Backfill success does not mean a chart is renderable unless stored candle coverage is sufficient.
- Chart data layers are consumed independently: `candles`, `trades`, `quotes`,
  and `events`. Indicator layers such as moving averages and VWAP are calculated
  from candle/trade/quote data by the chart engine or explicit downstream code,
  not by a separate preload-only API.
- Frontend requests use API/WebSocket only. It must not connect directly to
  Redis, S3, or ClickHouse.
- One accepted proposal should become one undo/redo unit.
- Drawing proposals are preview-first; applying a preview turns it into an editable drawing.
- Drawing `anchor.interval` and `sourceInterval` use canonical chart intervals.
- `parallelLineCount` is an integer from 2 through 10, and drawing `fillOpacity`
  is a number from 0 through 1.
- `riskRewardBox` uses exactly three canonical anchors in `[entry, stop, target]`
  order. Target time is normalized to Stop time; Stop and Target must remain on
  opposite sides of Entry.
- A trade-plan overlay anchors Entry to the confirmed completed candle. Its
  non-persisted future Stop/Target edge may use logical index only; it must not
  invent or persist a candle timestamp.
- An SMA cross keeps `timestamp` as the confirmation candle and stores
  `previousTimestamp`, `fraction`, and `price` for the actual interpolated
  SMA60/SMA120 intersection. The marker uses fractional logical index rather
  than the confirmation candle center.
- `fibonacciRetracement` uses exactly two canonical swing anchors and fixed v1
  levels `0, 0.236, 0.382, 0.5, 0.618, 0.786, 1`.

Update every mirror in the same change when this contract changes.
