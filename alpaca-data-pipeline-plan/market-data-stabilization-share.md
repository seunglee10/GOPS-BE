# Market Data Stabilization Share

> Superseded for the current chart-data rebuild.

The active market-data and chart contract is
`../docs/CHART_DATA_REBUILD_PLAN.md`.

This file no longer defines a preset chart universe, default watch list,
preload set, or hot-ranking seed. Current chart data starts empty and is created
only by explicit chart/backfill/live subscription flows.

Current durable rules:

- Redis keeps only recent chart state: latest 120 candles per symbol/timeframe,
  live provisional candle state, closed-candle replacement state, and backfill
  status.
- ClickHouse is the historical chart serving store.
- S3 final/manifest data is historical evidence for rebuild/materialization.
- Raw S3 is backup-only and must not become a serving source without a new
  reviewed design.
- Alpaca is called only for bounded missing ranges after Redis, ClickHouse, and
  S3 final/manifest checks.
- SIP and BOATS are mutually exclusive by time window; the same data must never
  be stored from both feeds.

Do not use older fixed-company, broad preload, or legacy replay assumptions from
archived notes when changing chart code.
