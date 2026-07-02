# Deprecated: S3-First Backfill Goal Plan

This file is retained only so older references do not break.
Do not use previous revisions of this file for chart work.

Use the current source of truth:

```text
../docs/CHART_DATA_REBUILD_PLAN.md
```

New chart direction:

- start from empty chart storage;
- never preload chart data outside explicit user requests;
- load only the requested symbol/timeframe/range/layer;
- keep Redis bounded to latest 120 candles per timeframe;
- replace provisional candles with confirmed Alpaca bars;
- use ClickHouse for older confirmed history;
- use S3 for durable evidence and rebuild;
- enforce exclusive SIP/BOATS writers.
