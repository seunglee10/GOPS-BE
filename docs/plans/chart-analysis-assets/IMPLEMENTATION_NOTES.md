# Chart Analysis Assets — Implementation Notes

This file records minimal adjustments made where the approved plan and current
repository contracts differ.

## Bundle 1 — Asset contract and analytics kernel

- Added `shared/chart-contract/chart-analysis-asset.schema.json` because the
  plan defines the asset as the canonical shared contract but does not assign a
  concrete schema path. It lives beside the canonical chart-command schema.
- A `kind="range"` trend carries `rangeFrom`, `rangeTo`, `rangeHigh`, and
  `rangeLow` in addition to the documented common trend fields. The current
  chart-engine `rangeBox` requires two complete time/price anchors, while a
  range intentionally has no pivot anchor IDs.
- Golden fixture JSON stores deterministic scenario recipes rather than long
  duplicated candle arrays. Tests expand those recipes only in the test
  process; production/local runtime never creates synthetic candles.
- Corrected the chart contract checker's existing tick TTL literal to match the
  canonical DDL (`TTL toDateTime(event_time) ...`). This changes no schema or
  retention behavior; it makes the documented parity command validate the DDL
  form already present in both copies.

## Bundle 2 — Rule compilers, build pipeline, and APIs

- The API and worker share `gops_agents.chart_assets.storage` and
  `gops_agents.chart_assets.progress` because the backend image already ships
  the agent shared package for existing entity resolution. This does not call
  or modify `AgentOrchestrator`, roles, providers, or legacy chart-command code.
- A first-time rule-only build stores an empty degraded agent layer plus the
  deterministic Korean fallback commentary. A rule-only rebuild preserves an
  existing agent layer, prompt version, and commentary exactly as specified.
- `coverage.missingBars` is the difference between the configured lookback and
  valid returned closed bars. The existing candle provider does not expose an
  exchange-calendar expected-row count at this boundary.
- The ClickHouse row adapter converts asset ISO timestamps to the database's
  `DateTime64` text format; timestamps inside the canonical JSON payload remain
  UTC ISO-8601 and unchanged.

## Bundle 3 — Frontend asset application and commentary UX

- The current `executeChartCommandGroup` implementation always recorded a
  chart-panel history entry even when every command had
  `historyScope="external"`. It now skips history only for all-external groups,
  so asset application remains atomic without polluting user undo; existing
  user and proposal groups retain their prior behavior.
- Non-chart panel renderers do not receive a chart document in the current
  workspace contract. `PanelWorkspace` therefore supplies the first active
  chart document and its candles as explicit read-only context to the
  commentary and operations panels. No extra chart document is created.
- The existing local Agent debug gate was private to `App.tsx`. Its unchanged
  URL/local-storage behavior was moved to `localAgentDebug.ts` so both the app
  and panel palette use one gate; production builds still keep the development
  panel hidden.
- The repository already has a custom chart test runner but no general React
  unit-test framework. Glossary matching and asset command behavior are pure
  modules imported by that runner, avoiding a new test dependency.
