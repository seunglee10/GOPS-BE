# Design Concept

## Purpose

This document records the design intent behind the current Kim Heejun branch changes.
It is meant to be used later during Codex-assisted merges by comparing this file with
another teammate's `DesignConcept.md`.

When code changes are made, update this document with:

- what changed
- why it changed
- how it was implemented
- which existing contracts must be preserved
- what tradeoffs or merge risks remain

The goal is not to duplicate every diff. The goal is to preserve the reasoning that a
future merge assistant needs in order to choose the best behavior when branches disagree.

## Current Branch Direction

This branch treats GOPS/Alfaka as a market-data-driven chart system. The central design
priority is to preserve the existing market data pipeline while integrating teammate UI
or agent features with minimum structural disruption.

Core rules:

- Historical candle initial loading is handled by REST `/api/charts/candles`.
- WebSocket is for live candle updates, reconnect gap-fill/control, and live delta flow.
- Chart API should read from Redis and ClickHouse, not directly from S3.
- ClickHouse `chart_candles` is the serving projection for chart snapshots.
- Redis is the hot/recent cache.
- S3 remains the durable storage and replay/rematerialization basis.
- Missing chart data should be filled through the backfill path, not by bulk historical
  loading over WebSocket.
- Avoid broad repository restructuring when integrating teammate work.

## Canonical Market Data Paths

Live path:

```text
Alpaca/Kafka raw
  -> Flink/local processor
  -> Kafka processed topics
  -> Redis hot/recent cache
  -> ClickHouse chart_candles serving projection
  -> S3 processed/final/live durable artifacts
```

Historical/backfill path:

```text
Alpaca Historical REST
  -> S3 raw archive
  -> batch transform/replay using the same processed candle contract
  -> S3 processed/final
  -> ClickHouse chart_candles materialization
```

Recovery/rematerialization path:

```text
S3 processed/final
  -> S3 replay/materialization job
  -> ClickHouse chart_candles
```

## Decision Log

### 2026-06-28: Selective integration of Cho Hyunho update candidate

Context:

- The remote branch most likely representing Cho Hyunho's work was identified as
  `origin/Helix`.
- The branch had no safe merge base with the current `kimheejun` branch and used a
  different top-level structure (`frontend/`, `backend/`), while this branch uses
  `apps/`, `packages/`, `services/`, and `infra/`.

Decision:

- Do not merge the whole branch mechanically.
- Selectively port useful behavior into the current branch structure.
- Preserve Kim Heejun's market-data/backfill/chart architecture and avoid moving files
  into the teammate branch layout.

Why:

- A full merge would risk deleting or replacing the current market-data implementation.
- The user requirement was to keep the current code usable, minimize impact on Kim
  Heejun code, and keep the existing code structure.

Merge implication:

- If a later merge compares this branch with a teammate branch, prefer behavior-level
  reconciliation over directory-level replacement.
- If the teammate branch has a better UI behavior, port it into the current structure
  instead of adopting a conflicting root layout.

### 2026-06-28: Historical snapshot moving averages

Context:

- Historical candles loaded through REST did not consistently provide moving average
  fields.
- The chart should render moving averages from the initial historical snapshot, not only
  from live deltas.

Decision:

- Add shared moving average attachment logic in `packages/alfaka/serving/moving_average.py`.
- Apply it in Redis, ClickHouse, and aggregate provider snapshot flows.
- Apply it to backfill processed candle generation as well.

Why:

- The chart data contract should be consistent regardless of whether data came from
  Redis, ClickHouse, or backfill processing.
- Frontend rendering should not need to know which backend store produced the candle.

Implementation shape:

- Preserve existing candle fields.
- Fill missing flat `ma5`, `ma20`, and `ma60` values from close prices.
- Do not overwrite existing moving average values when they already exist.

Merge implication:

- If another branch computes indicators differently, preserve the API-level guarantee
  that REST snapshot candles include usable moving average fields.
- Indicator calculation may be optimized later, but the snapshot contract should stay.

### 2026-06-28: 24-hour candle limit by interval

Context:

- The frontend previously requested a fixed `160` candles for minute charts.
- The requested behavior is to load a 24-hour window by default.

Decision:

- Centralize 24-hour candle count rules in backend and frontend helpers.
- Use interval-aware defaults:
  - `1m`: 1440
  - `5m`: 288
  - `10m`: 144
  - `1d`: 1
- Allow `/api/charts/candles` to omit `limit`; backend resolves the interval default.

Why:

- A hardcoded candle count hides the intended time window.
- Interval-aware limits make the UI and backend contract easier to reason about during
  future merges.

Implementation shape:

- Backend helper: `packages/alfaka/serving/intervals.py`.
- Frontend helper: `apps/chart-engine/src/intervals.ts`.
- Frontend `ChartPanel` requests the interval-derived limit.
- Backend route accepts `limit=None` and caps explicit limits at the backend maximum.

Merge implication:

- If another branch changes chart fetch behavior, prefer preserving the semantic rule
  "default chart snapshot equals roughly 24 hours" over preserving any numeric literal.

### 2026-06-28: Agent UI integration without chart pipeline disruption

Context:

- The teammate update included agent/chat interaction ideas and UI safety improvements.
- The current GOPS app already has chart panel and system area concepts.

Decision:

- Add a chart-to-agent reference flow without replacing the existing app layout.
- Add an app error boundary.
- Improve panel drop preview state while keeping current layout records.

Why:

- Agent features are useful, but they should attach to the existing chart document and
  panel model.
- UI enhancements should not alter market-data contracts or move the layout architecture.

Implementation shape:

- `Ask Agent 01` opens the agent area with an explicit chart reference.
- Empty agent draft sends a default "Analyze this chart." prompt.
- Agent fallback analysis requests produce at least one chart command.
- Drag/drop preview distinguishes valid, replace, and blocked states.

Merge implication:

- Preserve explicit chart reference selection when reconciling with another agent UI.
- Avoid implicit "first chart" behavior when a user initiated the request from a
  specific chart panel.

### 2026-06-28: Alpaca vs sample-looking data diagnosis

Context:

- Some symbols appeared to have similar chart shapes, raising concern that dummy data was
  being displayed instead of Alpaca data.

Observed state:

- ClickHouse `chart_candles` rows currently report `source=alpaca.bars` and `feed=sip`.
- Stored symbols have different price ranges and counts.
- The current stored range is short, roughly `2026-06-26 20:00` to `2026-06-26 23:59`,
  not a complete 24-hour historical backfill.
- The Docker `backfill-worker` was running, but logs did not show completed historical
  backfill work for the inspected state.
- The local smoke script can inject sample market data, so it should be treated as a dev
  validation tool, not proof of production Alpaca historical coverage.

Decision:

- Treat current chart data as Alpaca-sourced partial data unless `source`/`feed` says
  otherwise.
- Keep investigating historical backfill execution separately from live Alpaca websocket
  ingestion.
- Do not use sample smoke data as the final proof for real Alpaca historical backfill.

Merge implication:

- Future merge work should preserve explicit `source` and `feed` propagation.
- When validating data authenticity, inspect stored metadata and backfill job history,
  not only visual chart shape.

### 2026-06-28: One-year Alpaca historical storage with 24-hour default viewport

Context:

- The requested behavior is not to show a whole year compressed into the first chart
  screen.
- The system should store up to one year of `1m` historical candles per symbol, while
  the chart initially frames only the latest 24 hours.
- Users should be able to zoom out or pan backward and have older ranges loaded through
  REST, up to the stored one-year range.

Decision:

- Keep the chart serving contract as REST-first for historical data:
  `/api/charts/candles` owns initial snapshots and range pagination.
- Keep WebSocket scoped to live updates, reconnect gap-fill/control, and live delta
  behavior.
- Cap explicit chart candle requests at one year while keeping the omitted-limit default
  as an interval-aware 24-hour window.
- Store and serve canonical `1m` candles, deriving `5m` and `10m` snapshots from
  ClickHouse `1m` rows when those derived intervals are requested.

Why:

- A full one-year initial payload makes the default chart hard to read and slow to
  operate.
- Separating stored range from visible range preserves data availability without hurting
  first-screen readability.
- Deriving short aggregate intervals from canonical `1m` storage avoids forcing separate
  backfill jobs for every UI timeframe.

Implementation shape:

- Backend interval rules live in `packages/alfaka/serving/intervals.py`.
- Frontend interval rules live in `apps/chart-engine/src/intervals.ts`.
- Snapshot metadata reports requested/returned counts, available range, one-year target,
  and `hasMoreBefore`/`hasMoreAfter` pagination hints.
- `ChartPanel` keeps the initial viewport at 24 hours and requests older REST pages when
  the viewport moves near or beyond the loaded oldest candle.
- Snapshot loads merge by timestamp instead of replacing the existing candle store, so
  range pagination extends the chart history.

Merge implication:

- If another branch loads all data at once, preserve this branch's split between
  `stored candle range` and `initial visible range`.
- If another branch implements range loading differently, preserve the invariant that
  historical/range data comes through REST and not through bulk WebSocket replay.

### 2026-06-28: Backfill v1 materializes Alpaca data through S3 and ClickHouse

Context:

- Missing chart data for registry/watchlist symbols must be filled from Alpaca
  Historical REST, not hidden by dummy/sample data.
- The durable path must include S3 raw archive and S3 processed/final artifacts, with
  ClickHouse remaining the serving projection.

Decision:

- Backfill v1 uses:

```text
Alpaca Historical REST
  -> S3 raw archive
  -> shared bar-to-candle transform/schema
  -> S3 processed/final
  -> S3 processed object materialization into ClickHouse chart_candles
```

- `sample-dev` remains an explicit development/testing mode only.
- Missing Alpaca credentials produce a clear unavailable/failed status instead of falling
  back to sample data silently.

Why:

- Dummy data that looks like production data makes chart validation misleading.
- S3 is the durable replay source; ClickHouse is optimized serving state.
- Materializing processed S3 objects into ClickHouse lets recovery and backfill use the
  same candle contract as live processed data.

Implementation shape:

- `packages/alfaka/backfill/runner.py` fetches Alpaca bars, archives raw payloads,
  transforms to normalized candles, writes S3 processed/final, and materializes the
  processed object into ClickHouse.
- Backfill defaults now target a one-year lookback via `BACKFILL_DEFAULT_LOOKBACK_HOURS=8760`.
- Backfill status logic treats a partially returned 24-hour snapshot as chart-ready when
  ClickHouse coverage or succeeded job history proves the one-year target has been
  satisfied.

Merge implication:

- Keep S3 raw and processed/final writes in the canonical backfill path.
- Keep direct ClickHouse writes as the final materialization step or a development
  shortcut, not as the only durable backfill output.

### 2026-06-28: Watch List prices come from real market-data serving state

Context:

- Watch List cards showed `-- +0.00%`, which made missing prices look like valid flat
  market data.
- The Watch List should reflect the same Redis/ClickHouse market-data source used by the
  chart.

Decision:

- Build symbol summaries from Redis latest/recent data first, then fall back to
  ClickHouse latest `1m` candles.
- Show `No data` when no real price is available.
- Only show change percent when a real baseline price exists.
- Poll `/api/charts/symbols` periodically so cards refresh after backfill or live data
  arrival.

Why:

- Fake zero change is worse than an explicit empty state.
- Users compare Watch List cards with the chart; both must share the same data source and
  data authenticity rules.

Implementation shape:

- Backend summary logic lives in
  `services/07-api-websocket/gops-backend/app/services/alfaka_market_data.py`.
- Frontend rendering lives in `apps/gops-frontend/src/components/SystemArea.tsx`.
- The app-level symbol poll lives in `apps/gops-frontend/src/App.tsx`.

Merge implication:

- If another branch has prettier Watch List UI, preserve this branch's source-of-truth
  behavior: real Redis/ClickHouse price, explicit no-data state, no fake `0.00%`.

### 2026-06-28: OpenAI Agent credential lookup

Context:

- Agent features should fail clearly when `OPENAI_API_KEY` is absent.
- Local Docker and AWS deployment need an explicit non-hardcoded credential path.

Decision:

- Do not hardcode OpenAI credentials.
- Read `OPENAI_API_KEY` from process environment or a project `.env` discovered from the
  backend working directory upward.
- Local Docker should provide `OPENAI_API_KEY` through the root `.env` loaded by
  `docker-compose.yml`.
- AWS/Kubernetes should provide it through the optional `alfaka-openai-secret` mounted
  into the `gops-backend` deployment.

Why:

- The previous lookup could miss the repository-root `.env` depending on process working
  directory.
- The failure mode should be a clear 503 configuration error, not silent mock behavior.

Implementation shape:

- `.env` lookup is centralized in
  `services/07-api-websocket/gops-backend/app/core/config.py`.
- OpenAI calls in `services/07-api-websocket/gops-backend/app/services/ai_agents.py`
  call that lookup before making requests.
- `infra/k8s/base/secret.example.yaml` documents the secret keys, and
  `infra/k8s/base/deployment-gops-backend.yaml` references the optional secret.

Merge implication:

- Preserve explicit missing-key errors and optional secret wiring.
- Do not merge any branch that commits actual API keys or hides missing OpenAI
  credentials behind a production-looking mock response.

### 2026-06-28: Range loading only after explicit history navigation

Context:

- A default `1m` snapshot may return fewer than 1440 candles because equity bars only
  exist during trading sessions.
- Treating "fewer than 1440 returned rows" as missing visible capacity caused the
  frontend to fetch older REST pages during the default screen.

Decision:

- Do not auto-load older pages only to fill the 24-hour default count.
- Trigger REST range loading when the user zooms out beyond the default interval window
  or pans into already-loaded history near the oldest candle.
- Deduplicate in-flight range requests by symbol/timeframe/oldest timestamp rather than
  by page size.

Why:

- The first screen must stay a recent-window view, not silently expand into older trading
  sessions just because non-trading minutes are absent.
- User intent to inspect more history is expressed through zoom/pan.

Merge implication:

- If another branch aggressively preloads history, keep that preload invisible to the
  default viewport or make it an explicit user action.
- Preserve REST as the historical range-loading mechanism.

### 2026-06-28: Mobile workspace stacks instead of compressing the full grid

Context:

- At narrow mobile widths, preserving the full five-column workspace grid compressed
  chart and system panels until headers and Watch List prices overlapped.

Decision:

- Keep the desktop bento/grid workspace at normal widths.
- At small mobile widths, stack panels vertically and give chart/system panels stable
  minimum heights.
- Add `data-panel-type` to panel cards so CSS can target chart panels without changing
  the layout data model.

Why:

- The mobile goal is readable access, not preserving the exact desktop spatial layout at
  unusable widths.
- This keeps the current panel model intact while preventing visible overlap.

Merge implication:

- If another branch has a richer mobile layout, prefer whichever version preserves panel
  readability and avoids text overlap without restructuring the shared layout model.

## Verification Baseline

The following checks passed after the current changes:

```sh
env PYTHONPATH=packages python -m compileall -q packages services/07-api-websocket/gops-backend/app tests
env PYTHONPATH=packages python -m unittest discover tests
env PYTHONPATH=packages:services/07-api-websocket/gops-backend python -m unittest discover services/07-api-websocket/gops-backend/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
docker compose config --quiet
kubectl kustomize infra/k8s/base
kubectl kustomize infra/k8s/overlays/aws
git diff --check
```

Runtime checks performed:

- Docker backend `/health` returned OK.
- `/api/charts/candles?symbol=AAPL&interval=1m` returned candles with `ma5`, `ma20`,
  and `ma60`.
- ClickHouse `chart_candles` contained one-year-range `1m` rows for the active
  Watch List symbols AAPL, TSLA, and NVDA, with ranges beginning on 2025-06-27 and
  ending on 2026-06-26.
- MinIO contained S3 raw archive objects under `market-data/raw/alpaca` and
  S3 processed/final objects under `market-data/final`.
- Browser at `http://localhost:5173/` rendered the chart canvas.
- Browser console had no error logs during the checked flow.
- `Ask Agent 01` opened the agent area with the AAPL chart reference.
- Watch List cards displayed real Redis/ClickHouse-backed prices instead of `--`.
- Desktop and mobile browser checks showed no horizontal overflow; mobile stacks panels
  vertically instead of compressing the desktop grid.

## Ongoing Update Rules

For every future code change in this branch:

1. Add or update a decision-log entry in this file.
2. Focus on intent and merge implications, not just file names.
3. State whether the change preserves, narrows, or changes an existing contract.
4. If a teammate implementation conflicts, describe which behavior should win and why.
5. Keep local-only artifacts, credentials, and generated outputs out of this document.
