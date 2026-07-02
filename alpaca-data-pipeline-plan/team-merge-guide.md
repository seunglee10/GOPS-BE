# Team Merge Guide For Chart Data Rebuild

> Current source of truth: `../docs/CHART_DATA_REBUILD_PLAN.md`.

When merging frontend, API, market-data, or platform work, keep the current
on-demand chart contract:

- no preset company universe
- no default watch-list/company seed in backend runtime
- no broad preload on startup
- chart requests read Redis/ClickHouse and trigger bounded backfill only when
  data is missing
- Redis keeps latest 120 candles per symbol/timeframe and live replacement state
- ClickHouse owns historical chart serving
- S3 final/manifest is checked before Alpaca
- raw S3 remains backup-only
- SIP and BOATS never write overlapping data

Preserve team-owned UI/agent/order work unless a chart-data contract requires a
small connection change. Do not reintroduce older fixed-company or preload
behavior during conflict resolution.
