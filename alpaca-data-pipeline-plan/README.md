# Alpaca Data Pipeline Plan

The active chart-data rebuild plan is:

- `../docs/CHART_DATA_REBUILD_PLAN.md`

Files in this folder are historical handoff notes only. They must not define
active chart symbols, default company lists, preload ranges, Kafka topics,
Redis keys, S3 prefixes, ClickHouse tables, or backfill source order.

Current chart direction:

- no preset chart universe
- no default company preload
- Redis latest 120 candles per symbol/timeframe
- ClickHouse historical serving
- S3 final/manifest evidence before Alpaca
- raw S3 backup-only
- SIP/BOATS single-feed operation by time window

Archive material is useful for understanding why earlier decisions changed, but
the current rebuild must follow `docs/CHART_DATA_REBUILD_PLAN.md`.
