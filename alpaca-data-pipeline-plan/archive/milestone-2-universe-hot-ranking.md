# Milestone 2 Historical Note

This milestone described an earlier universe/hot-ranking implementation and is
not an active chart-data contract.

Current chart rebuild rules are in
`../../docs/CHART_DATA_REBUILD_PLAN.md`:

- backend runtime starts with no preset chart companies
- symbols are introduced by explicit chart, watch, hot, live, or backfill flows
- no archived default company list should be copied back into config, tests, or
  documentation
