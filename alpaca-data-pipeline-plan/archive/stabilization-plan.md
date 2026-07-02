# Stabilization Plan Historical Note

This archive previously contained long-running market-data stabilization notes.
It is no longer an implementation source for chart development.

Use `../../docs/CHART_DATA_REBUILD_PLAN.md` for the current rebuild.

Current non-negotiables:

- no preset chart-company universe
- no backend default watch-list seed
- no startup preload
- Redis latest 120 candles per symbol/timeframe only
- ClickHouse historical serving
- S3 final/manifest before Alpaca
- raw S3 backup-only
- SIP/BOATS mutual exclusion
