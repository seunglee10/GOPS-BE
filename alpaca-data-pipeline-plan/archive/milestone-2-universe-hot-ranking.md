# Milestone 2 Universe And Hot Ranking

Date: 2026-06-30
Status: local implementation and browser gate passed; AWS subscription/runtime verification deferred by user instruction

## Scope Completed

- Added S&P 500 registry config at `systems/market-data/config/sp500-universe.json`.
- Changed Alpaca collection defaults so full-universe `bars`, `updatedBars`, `dailyBars`, and `statuses` use the S&P 500 registry.
- Kept `ALPACA_SYMBOLS` as the default Watch List seed: `AAPL`, `MSFT`, `NVDA`, `AMZN`, `META`, `GOOGL`, `TSLA`.
- Added dynamic trade-tier resolution for active chart symbols, watchlist symbols, and Hot Ranking symbols, with active > watchlist > hot priority and optional `ALPACA_MAX_TRADE_SYMBOLS`.
- Added Redis/control-plane keys for watchlist and hot tier state.
- Added `GET /api/charts/hot-symbols`.
- Implemented Hot Ranking as current-session dollar-volume ranking:
  - Redis snapshot first.
  - ClickHouse single aggregate query second.
  - Per-symbol fallback scan only as a degraded last resort.
- Added frontend `hotRanking` as a first-class workspace panel in the panel registry, catalog, and default Chart layout.
- Added Hot Ranking row selection so choosing a hot symbol updates the active chart.
- Updated env/docs/k8s/compose contracts for S&P 500, Hot tier, and trade subscription cap settings.

## Checks Passed

- `python -m unittest discover systems/market-data/tests`: 65 tests passed.
- `python -m unittest discover systems/api-server/tests`: 31 tests passed.
- `npm run test:chart --prefix apps/gops-frontend`: passed, with the existing localstorage-file warning.
- `npm run build --prefix apps/gops-frontend`: passed.
- `python -m compileall systems/market-data/shared systems/api-server/pods/api-server/gops-backend/app`: passed.
- `docker compose config --quiet`: passed.
- `kubectl kustomize infra/k8s/base`: passed.
- `kubectl kustomize infra/k8s/overlays/aws`: passed.
- `git diff --check`: passed.

## Local API Smoke

- `GET /health` returns `{"status":"ok","service":"gops-backend"}`.
- `GET /api/charts/symbols` returns the seven mega-cap Watch List seed symbols.
- `GET /api/charts/hot-symbols?limit=3` returns ranked S&P 500 symbols with `rankReason=clickhouse_1m_session_aggregate`.
- The Hot Ranking API initially exposed a slow degraded path when Redis had no snapshot; this was fixed by adding the ClickHouse single aggregate query before per-symbol fallback scanning.

## Browser Smoke

Opened `http://127.0.0.1:5173` in the in-app browser.

- Default Chart layout shows the `Hot Ranking` workspace panel.
- Hot Ranking rendered ranked symbols, price/change values, and compact dollar volume.
- Selecting `MU` from Hot Ranking updated the active chart and search symbol to `MU`.
- Desktop Hot Ranking rows had no measured horizontal overflow.
- Mobile-width smoke showed Hot Ranking remained visible with no measured row overflow.
- Browser console warning/error baseline was empty during the checks.
- Existing Watch List continued to render separately from Hot Ranking.

## Remaining Gate

- AWS cluster access is blocked by the same EKS access principal mismatch documented in Milestone 1, so the real Alpaca subscription and deployed ingestor trade-tier behavior were not verified against the cluster.
- The user instructed not to use EKS and to continue by assuming the AWS deployment shape, so this gate is deferred rather than blocking local Milestone 3+ work.
- If AWS verification is reopened later, prove S&P 500 full-universe bars/statuses and tiered trades in AWS, including the one-symbol trace carried forward from Milestone 1.
- If Alpaca subscription limits reject one full S&P 500 bar/status connection, shard subscriptions by config rather than hardcoding symbol subsets.
