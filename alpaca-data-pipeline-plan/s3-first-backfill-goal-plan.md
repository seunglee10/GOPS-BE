# On-Demand Chart Backfill Historical Note

> Superseded for the current chart-data rebuild.

Use `../docs/CHART_DATA_REBUILD_PLAN.md` for implementation. This historical
plan no longer defines active symbols, default watch lists, preload scope, or
browser-smoke symbols.

Current implementation direction:

- Start with no preloaded chart companies.
- Load chart data only when the frontend requests a symbol/timeframe/range or an
  operator explicitly runs a backfill job.
- Keep Redis limited to latest 120 candles per symbol/timeframe plus live and
  replacement state.
- Serve older confirmed history from ClickHouse.
- Check S3 final/manifest before Alpaca for missing ranges.
- Keep raw S3 as backup-only until a separate design promotes it.
- Prevent duplicate storage through deterministic keys and idempotent writes.
- Keep SIP and BOATS mutually exclusive so both feeds never write the same data.

Any new Goal Mode prompt must be written from the active rebuild plan, not from
this archived document.
