# GOPS Shared Chart Contract

Shared chart-command contract for frontend runtime and backend/agent code.

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
- Canonical intervals are `1m`, `5m`, `10m`, `1D`, `1W`, `1M`.
- Candle readiness uses both `dataStatus` and detailed `coverage`.
- Backfill success does not mean a chart is renderable unless stored candle coverage is sufficient.
- The planned chart-data rebuild starts from empty storage and may return empty/partial coverage while bounded backfill is queued.
- Redis is only the newest 120-candle cache and realtime/replacement state; older confirmed candles come from ClickHouse.
- Live candle events may be provisional first and then be replaced by confirmed `bars`, `updatedBars`, or `dailyBars` for the same timestamp.
- Chart payloads must preserve `feedProfile`, `marketSession`, and planned `feedEpoch` metadata so SIP/BOATS overlap can be rejected.
- One accepted proposal should become one undo/redo unit.
- Drawing proposals are preview-first; applying a preview turns it into an editable drawing.

Update every mirror in the same change when this contract changes.
