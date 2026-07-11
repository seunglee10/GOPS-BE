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
- `chartAssetOps` is always present in the panel palette and renders in local
  Vite, Docker production builds, and deployed production. Its `(개발)` suffix
  identifies a manual operations tool and is not a visibility gate. The
  separate `localAgentDebug.ts` helper remains scoped only to Agent request
  payload diagnostics.
- The repository already has a custom chart test runner but no general React
  unit-test framework. Glossary matching and asset command behavior are pure
  modules imported by that runner, avoiding a new test dependency.
- The three chart-analysis layer toggles are icon-only and sit above the time
  axis. Their user-facing meanings are support/resistance, trend, and insight;
  the third keeps the canonical internal `agent` layer key without presenting
  an `AI` badge.
- Applying system-owned chart-analysis drawings restores the chart's prior
  interaction mode after the add batch. Initial asset loading therefore stays
  in Pan, while user-created drawings retain the existing temporary Select
  behavior.

## Bundle 4 — Agent layer and Korean commentary

- The independent builder now constructs its own `ChartAssetLLMService`; this
  keeps the worker executable with the documented environment variables and
  does not modify or call orchestrator workflows, roles, providers, or legacy
  chart-command code.
- Strict Responses API validation is mirrored locally before intent
  compilation. This keeps mock tests deterministic and records rejected
  anchors, duplicate rule lines, and visual-budget overflow in
  `layers.agent.droppedIntents` without accepting invented coordinates.
- The plan reserves agent-layer metadata without prescribing a warning field.
  Numeric grounding warnings and degraded failure reasons are stored in the
  schema-compatible `layers.agent.meta` object as `groundingFlags` and
  `failureReason`.
- Rule-only rebuilds preserve the complete existing recommendation list along
  with the existing agent layer, prompt version, and commentary; fresh LLM
  builds merge rule and LLM recommendations deterministically with a two-item
  cap.
- The local environment has no `OPENAI_API_KEY`, so Bundle 4 used strict mocked
  Responses API tests plus a real-candle Kafka-to-worker-to-ClickHouse run. The
  run finished `completed_with_errors` and stored NVDA 1D as `degraded` with
  `prompt-v1`, no agent drawings, `missing_openai_api_key`, and deterministic
  Korean fallback commentary; its failed item remains available for retry.
- The exact market-data validation command in 07 §2 discovered that the
  pre-existing derived-service test imported backend `app` without adding its
  repository path. The test now adds the same backend root used by neighboring
  market-data tests, so the documented command runs without a caller-provided
  `PYTHONPATH`; production imports and behavior are unchanged.

## Refine review

- Adopted P1 and R1: the builder requests one extra aggregate and removes 1W/1M
  buckets whose calendar period has not ended; range fallback now measures the
  final 40% of the display window instead of the full lookback. Boundary,
  full-lookback, pivot-price, and level-price tests cover both corrections.
- Adopted R2 and R3: OpenAI retry scope ends after remote response validation,
  so deterministic compiler/grounding defects surface without a second API
  call. Invalid Kafka envelopes are logged, committed, and skipped, while a
  valid job runtime failure stays uncommitted for redelivery.
- Adopted R4 through R6: asset-cache invalidation uses generation guards so an
  old in-flight response cannot repopulate the cache; ambiguous bare Korean
  glossary aliases were replaced by explicit 양봉/음봉 entries; API-side Redis
  progress and Kafka producer factories are process-cached.
- Adopted the actionable portion of R7/R8: golden prices and display/lookback
  separation are fixed in tests, and route coverage now includes auth 401,
  storage 503, strict S&P 500 expansion, invalid intervals, envelope options,
  and public progress lookup after enqueue failure. Live Redis pubsub relay is
  retained as an integration check rather than simulated by private state.
- Adopted M1, M2, M8–M13, and M15–M17 where behavior was unambiguous: higher-TF
  absence lives only in `buildContext.flags`; event labels preserve direction;
  recommendations are defensively capped; candle-close updates do not reapply
  assets; clear-all preserves asset drawings; stale/as-of presentation is
  shared; operations polling and numeric input are stable; empty layers disable
  toggles; S&P 500 loading is strict and single-pass; typography uses its role
  token; higher-timeframe prompt levels include support/resistance when known.
- M3 was not changed because the approved contract intentionally defines 1D
  regime 52-week statistics separately from 1W/1M event lookback semantics.
  M5 was not changed because the builder rejects fewer than 20 candles and an
  empty range would require invented anchors. M6 remains the specified
  low-support/high-resistance candidate model. M7 remains uncapped so every
  failed item is retryable; the list is bounded by the 1,500-item job contract
  and the Redis key has a one-day TTL.
- M4 is documented rather than moved: ATR remains analytics-owned so the
  deterministic kernel does not alter the existing serving indicator contract.
  M14's fallback remains available to existing general market-data callers,
  while the chart-asset `"sp500"` build explicitly disables that fallback and
  returns 503 when the registry file is unavailable.

## Remote UI integration

- The feature baseline was merged into `origin/dev` at `ce1d8a5` with the
  remote branch as first parent. Remote app shell, layout-edit flow, 8x6 grid,
  responsive cell floors, preset dock, bottom command bar, and shared panel UI
  remain authoritative; only the two chart-asset panel kinds and their
  read-only active-chart context were added at shared integration points.
- `styles.css` remains byte-identical to the remote UI baseline. Chart-owned
  geometry and interaction differences live in `chart-features.css`, which is
  imported after the remote stylesheet and rejects shell/layout selectors.
  New UI and canvas text use the remote semantic typography roles rather than
  the removed 10/12px compatibility sizes.
- The remote legacy chart agent and `gops_agents/chart_command` package are
  intentionally unmodified. A feature-baseline change to the legacy Python
  schema and its coupling test were dropped during integration. The historical
  frontend `pointMarker` reference remains a type-only compatibility member;
  the canonical asset and shared chart-command contracts continue to emit the
  new drawing types and never depend on or call the legacy agent path.
- Mobile snapshots from the feature baseline were not integrated. This merge
  validates only the requested desktop surface; mobile behavior remains the
  remote baseline pending a dedicated review.
