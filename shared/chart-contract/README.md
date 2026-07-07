# GOPS Shared Chart Contract

Shared chart-command contract for frontend runtime and backend/agent code.

Chart data storage and transport semantics are defined by
`docs/CHART_DATA_REBUILD_PLAN.md`. This contract covers UI/chart command shape;
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

Update every mirror in the same change when this contract changes.
