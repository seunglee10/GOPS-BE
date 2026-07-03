# GOPS Chart Engine

Browser TypeScript engine for chart state, commands, indicators, scaling, and rendering.
React UI code stays in `apps/gops-frontend`.

## Owns

- `ChartDocument` and chart command reducer.
- Candle snapshot/live event normalization.
- Indicator and viewport state.
- Render scene generation and Canvas 2D rendering helpers.
- Symbol/watchlist client normalization.

## May Edit

```text
apps/chart-engine/src/
apps/chart-engine/package.json
apps/chart-engine/tsconfig.json
shared/chart-contract/
```

## Coordinate Before Editing

```text
apps/gops-frontend/src/
systems/api-server/
systems/market-data/
```

The chart engine does not read Alpaca, Kafka, S3, Redis, or ClickHouse directly.
It receives historical data from backend REST APIs and live data from WebSocket events.
