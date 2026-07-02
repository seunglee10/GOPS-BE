# Deprecated: AWS Market Data Reset Runbook

This runbook is retained only so older references do not break.
Do not use previous revisions of this file for chart work.

Use the reset scope in:

```text
../docs/CHART_DATA_REBUILD_PLAN.md
```

The new reset policy is intentionally chart-scoped:

- scale down chart market-data writers/workers first;
- reset only chart market-data Redis keys and ClickHouse chart tables;
- use a fresh S3 prefix for the new on-demand rebuild;
- preserve auth, order, agent, symbols, and news data unless an operator
  explicitly asks for a broader reset.
