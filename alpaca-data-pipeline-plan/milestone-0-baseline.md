# Milestone 0 Baseline

Date: 2026-06-30
Status: local baseline captured; AWS cluster access unavailable in this environment

## Scope

This baseline was captured before implementation changes for Milestone 1. It records the current repository, runtime, tests, and browser state for the Alpaca market-data stabilization Goal.

## Required Documents Read

- `AGENTS.md`
- `docs/PRODUCT_CONTEXT.md`
- `docs/STRUCTURE_GUIDE.md`
- `docs/ARCHITECTURE.md`
- `docs/IMAGE_STRATEGY.md`
- `docs/ENVIRONMENT.md`
- `alpaca-data-pipeline-plan/stabilization-plan.md`

## Current Runtime Evidence

- Docker is running.
- Local containers are already up for backend, frontend, Kafka, Redis, ClickHouse, processor, loaders, sinks, backfill worker, and order workers.
- Local API responds: `GET /health` -> `{"status":"ok","service":"gops-backend"}`.
- Local frontend responds on `http://localhost:5173`.
- `GET /api/charts/symbols` returns the current semiconductor seed/watchlist set: `NVDA`, `AMD`, `AVGO`, `TSM`, `ASML`, `AMAT`, `MU`.
- `GET /api/charts/hot-symbols` returns `404`; the planned Hot Ranking API is not implemented yet.
- `GET /api/charts/candles?symbol=NVDA&interval=1m&limit=5` returns `dataStatus=partial`, `sourceInterval=1m`, `returnedCount=5`, `storedCandleCount=236331`, and `targetStoredCount=98280`.
- The current `targetStoredCount=98280` reflects the existing one-year `1m` target, not the planned three-year target.

## Browser Baseline

Opened `http://localhost:5173` in the in-app browser.

- Page title: `GOPS Layout Runtime`.
- Seven workspace panels are present: notifications, primary chart, proposal review, watchlist, news feed, symbol summary, and order.
- Chart canvas is present and nonzero sized.
- Watchlist rows render the seven current seed symbols.
- No `Hot Ranking` panel or text is present.
- Browser console warning/error baseline was empty during this check.

## Current Architecture Findings

- `docker-compose.yml` includes `local-stream-processor` running `systems/market-data/pods/market-processor/local_main.py`.
- `docs/ARCHITECTURE.md` and `docs/IMAGE_STRATEGY.md` describe a `market-processor` pod/runtime.
- `infra/k8s/base/kustomization.yaml` does not include a processor deployment.
- `infra/k8s/overlays/aws/kustomization.yaml` maps the `gops-market-processor` image but does not include a processor deployment resource.
- `infra/k8s/base/deployment-local-stream-processor.example.yaml` exists but is not part of base or AWS overlay rendering.
- This mismatch is a leading P0 suspect for the reported AWS realtime no-data failure.

## Current Contract Gaps To Carry Forward

- Universe config is still `semiconductor-100`; S&P 500 registry/config is not implemented.
- Ingestion still defaults to seed symbols for full bar/status channels and active-chart-only trades.
- Kafka consumers use `auto_offset_reset=latest` and `enable_auto_commit=True`.
- Backfill queue uses Redis list push/pop semantics, not Redis Streams.
- Redis live candle key is still `candle:{symbol}:1m:live`.
- Redis recent candle series have TTL but no explicit per-interval max-count trim.
- Historical target helpers still encode one-year `1m` and five-year higher timeframe assumptions.
- ClickHouse serving reads do not yet use a deterministic latest-row projection for corrections and duplicates.
- `hotRanking` frontend panel type is not implemented.

## Checks Run

- `python -m unittest discover systems/market-data/tests`: 53 tests passed.
- `python -m unittest discover systems/api-server/tests`: 29 tests passed.
- `npm run test:chart --prefix apps/gops-frontend`: passed, with an existing localstorage-file warning.
- `python -m compileall -q systems`: passed.
- `npm run build --prefix apps/gops-frontend`: passed.
- `docker compose config --quiet`: passed.
- `kubectl kustomize infra/k8s/base`: passed.
- `kubectl kustomize infra/k8s/overlays/aws`: passed.
- `git diff --check`: passed.

## AWS Access

- `kubectl config current-context` reports that no current context is set.
- `kubectl config get-contexts` returns no configured contexts.
- No live AWS cluster one-symbol trace was possible in this environment.
- The AWS realtime trace remains a required verification gate for later milestones and Goal closure.

## Milestone 0 Exit

Milestone 0 local baseline is complete. Proceed to Milestone 1 by closing the live data path, starting with the explicit Python processor runtime gap in k8s/AWS and the one-symbol raw-to-processed trace contract.
