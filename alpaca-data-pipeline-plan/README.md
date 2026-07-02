# Deprecated Alpaca Data Pipeline Plans

This folder is no longer the source of truth for chart work.
It is retained only so older links do not break.

Do not use previous revisions of files in this folder for chart work.

Use this document instead:

```text
../docs/CHART_DATA_REBUILD_PLAN.md
```

Current chart direction:

- empty chart-data start;
- no universe preload;
- on-demand chart backfill only;
- Redis latest 120 candles per timeframe;
- ClickHouse older confirmed history;
- S3 durable evidence;
- exclusive SIP/BOATS feed ownership.
