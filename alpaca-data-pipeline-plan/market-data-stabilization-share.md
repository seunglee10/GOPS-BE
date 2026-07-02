# Deprecated: Market Data Plan Redirect

This file is retained only so older references do not break.
Do not use previous revisions of this file for chart work.

Use the current chart-data rebuild plan instead:

```text
../docs/CHART_DATA_REBUILD_PLAN.md
```

The current rule is simple: chart data starts empty, Redis stores only the
latest 120 candles per timeframe, older confirmed candles come from ClickHouse,
S3 is durable evidence, and missing ranges are filled on demand.
