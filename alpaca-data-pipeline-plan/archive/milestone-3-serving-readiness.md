# Milestone 3 Deterministic Serving And Readiness

Date: 2026-06-30
Status: local implementation, automated test gate, API smoke, and browser smoke passed; performance/AWS verification deferred

Note: the original 3-year intraday target from this milestone was superseded on 2026-06-30. `1D` remains a 3-year target, but `1m`/derived intraday preload is now scoped to the 2026-06 operating window back through 2025-04 inclusive.

## Scope Completed

- Updated backend and chart-engine historical target helpers to the v1 preload contract:
  - `1m=122850`
  - `5m=24570`
  - `10m=12285`
  - `1D=756`
  - `1W=156`
  - `1M=36`
- Added `historical_target_bars` and kept `candle_count_for_1y` only as a compatibility alias.
- Added Redis closed-candle trim caps:
  - `1m=780`
  - `5m=156`
  - `10m=78`
  - `1D=756`
  - `1W=156`
  - `1M=36`
- Updated the Python processor Redis write path so recent closed candle zsets are trimmed by interval cap after each insert/correction.
- Added deterministic ClickHouse latest-row source queries using `(symbol, normalized interval, event_time)` with `inserted_at DESC, source_event_id DESC`.
- Routed direct `1m`/`1D` reads, derived `5m`/`10m`/`1W`/`1M` aggregations, coverage, and Hot Ranking ClickHouse fallback through the deduped source query.
- Added `repairStatus` to chart snapshot metadata and frontend normalization/runtime state:
  - `none`
  - `gapfill_required`
  - `gapfill_active`
  - `gapfill_failed`
  - `history_preload_required`
- Kept `dataStatus` focused on renderability. A requested range can now be `ready` while broader canonical preload coverage reports `repairStatus=history_preload_required`.

## Checks Passed

- `PYTHONPYCACHEPREFIX=.pycache PYTHONPATH=systems/market-data/shared .venv/bin/python -m unittest systems/market-data/tests/test_market_data_hardening.py`: 61 tests passed.
- `PYTHONPYCACHEPREFIX=.pycache .venv/bin/python -m unittest systems.api-server.tests.test_market_data_query`: 24 tests passed.
- `npm run test:chart` from `apps/gops-frontend`: passed, with the existing localstorage-file warning.
- `npm run build` from `apps/gops-frontend`: passed.
- `PYTHONPYCACHEPREFIX=.pycache PYTHONPATH=systems/market-data/shared .venv/bin/python -m compileall -q systems/market-data/shared systems/api-server/pods/api-server/gops-backend/app`: passed.
- `git diff --check`: passed.
- Local latest-code API smoke on `127.0.0.1:8010` passed:
  - `/health` returned ok.
  - `1m` candle response returned the then-current target count; current target is `targetStoredCount=122850` after the scoped intraday preload update.
  - `1M` derived candle response returned `targetStoredCount=756` and `repairStatus=none`.
- Local browser smoke on `127.0.0.1:5174` passed:
  - Switched `1m`, `5m`, `10m`, `1D`, `1W`, and `1M`; each kept a visible chart with no candle API error, no empty state, and no preparing-data loop.
  - Hot Ranking remained visible.
  - Selecting `MU` from Hot Ranking updated the active chart to `MU`.
  - Browser console warning/error check returned empty.

## Remaining Gate

- Benchmark or smoke the latest-row ClickHouse query against realistic row counts before deciding whether Milestone 7 needs a materialized serving projection.
- AWS/EKS runtime verification remains deferred by user instruction; continue assuming the AWS deployment shape until the user reopens that gate.
