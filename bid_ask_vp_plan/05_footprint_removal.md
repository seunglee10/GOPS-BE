# 05 — Legacy footprint removal (complete list)

User decision: **full removal in this rollout** — UI option, REST endpoint, derived-worker kind,
and dig→footprint rendering. Reusable logic (side classification) is *moved* (see `01` §1.2), not
kept in place. Execute this stage **after** the new order-flow paths are implemented and passing
(ordering in `06`), so the tree never lacks both features at once.

General rule: after each numbered group below, run the compile/test gate from `06` §6 before
continuing. When a symbol has more callers than listed here, follow the callers — this list was
built from the current tree but the tree moves.

## 1. Backend — market-data

| Target | Action |
|---|---|
| `shared/alfaka/serving/footprint.py` | **Delete** after moving `classify_trade_side` + needed normalizers to `alfaka/orderflow/classification.py` (`01` §1.2). Constants `FOOTPRINT_*` die with it. |
| `shared/alfaka/serving/chart_derived_data.py` | Remove the `footprint` kind end-to-end: `build_footprint_request`, footprint cache-key branch (`chart:footprint:*`), footprint entries in `redis_ttl_seconds` / `artifact_retention_seconds`, and the `CHART_FOOTPRINT_CACHE_TTL_SECONDS` / `CHART_FOOTPRINT_ARTIFACT_RETENTION_SECONDS` env reads. `indicators` and `volumeProfile` kinds must remain byte-compatible. |
| `pods/chart-derived-data-worker/main.py` | Remove `compute_footprint_request` and its dispatch branch. Worker keeps serving indicators + volumeProfile. |
| `shared/alfaka/serving/clickhouse_provider.py` → `footprint_ticks` | **Delete** (grep for callers first; the EOD rollup uses its own hourly-window queries, not this). Same for the `redis_provider.footprint_ticks` variant if present, and the `footprint_ticks` passthrough on `serving/provider.py`. |
| `shared/alfaka/serving/dto.py` | No change (`volume_profile_event` stays, reserved/unused). |
| Tests | Delete `tests/test_footprint.py` (assertions live on in `test_orderflow_classification.py` / `test_orderflow_rollup.py`). In `tests/test_chart_derived_data_worker.py` remove footprint cases/fakes. In `tests/test_market_data_hardening.py` update any assertion referencing footprint envs/keys/kinds (grep `footprint` across `systems/market-data/tests/`). |

## 2. Backend — api-server

| Target | Action |
|---|---|
| `app/market_data/query/routes.py` | Remove `GET /api/charts/footprint`. |
| `app/market_data/query/service.py` | Remove `footprint_series` and the footprint import surface. |
| Tests | In `systems/api-server/tests/test_market_data_query.py`: remove `test_footprint_series_*`, `FakeFootprintProvider`, and footprint branches of `FakeDerivedClient`. Grep `footprint` across `systems/api-server/`. |

Not applicable (verified absent): footprint has no Kafka topic, no ClickHouse table, no
subscription hook — nothing to remove in `platform/kafka/topics.txt` or initdb SQL.
`chart_derived_artifacts` rows of kind `footprint` expire by their own `expires_at` TTL; no
cleanup needed.

## 3. Config / env / docs

| Target | Action |
|---|---|
| `docker-compose.yml`, `infra/k8s/base/app/configmap.yaml`, `infra/k8s/overlays/aws/configmap-aws-patch.yaml`, `.env.example` | Remove `CHART_FOOTPRINT_CACHE_TTL_SECONDS`, `CHART_FOOTPRINT_ARTIFACT_RETENTION_SECONDS` (grep `FOOTPRINT` in `infra/` and compose). |
| `docs/CHART_DATA_REBUILD_PLAN.md` | Remove `GET /api/charts/footprint` from the route inventory and the footprint mentions in the derived-worker section; add the order-flow routes/event (per `03`). |
| `docs/ENVIRONMENT.md` | Remove footprint envs; add `ORDER_FLOW_*` envs. |
| `docs/cdc.md` | Update the footprint-estimation paragraphs to point at the order-flow feature (the statement "bid/ask delta can only be estimated from trades+quotes" stays true — reword around `alfaka.orderflow`). |
| `docs/about_front.md` | Panel list: add 오더플로우; remove footprint interval mentions if any. |

## 4. Frontend — gops-frontend + chart-engine

| Target | Action |
|---|---|
| `src/chart/types.ts` | `ChartInterval`: remove `"footprint"`; remove it from `chartIntervals` and `defaultVisibleBarsByInterval`. Delete `FootprintPriceLevelDto`, `FootprintBucketDto`, `FootprintResponseDto`, `FootprintQuery` type surface; delete `ChartState.footprint`. |
| `chart-engine/src/intervals.ts` | Remove `"footprint"` from its interval list and `normalizeChartInterval` acceptance. |
| `src/chart/chartDocumentAdapter.ts` | `normalizeFrontendInterval`: **map legacy `"footprint"` → `"1m"`** (persisted layouts/documents may still carry it; they must load, not crash). Add a test for this migration. |
| `src/chart/cdcClient.ts` | Remove `fetchFootprint`, `FootprintQuery`, `normalizeFootprintResponse`, `isFootprintBucket`. |
| `src/chart/ChartCanvas.tsx` | Remove: the `interval === "footprint"` branch in `drawBasePriceLayer`; `drawFootprintBuckets`, `drawFootprintBucket`, `drawFootprintGhostCandle` (port visuals into `drawOrderFlowColumns`' ghost fallback first), `drawFootprintEmptyState`, `drawCenteredFootprintCandle`, `drawFootprintCandleShape`, `drawFootprintEstimatedLabel` (superseded by `drawEstimatedBadge`), `drawSemanticFootprint` + footprint arm of `drawSemanticPlaceholder`, and the `footprintBucket*`/`footprintLevel*` tuning consts. |
| `src/components/ChartPanel.tsx` | Remove `footprint` state + fetch effect + `renderChart` merge (replaced per `04` §5.2). |
| Dig → footprint (`src/chart/semanticTimeline.ts` and friends) | Remove the footprint semantic unit kind (`unit.kind === "footprint"`, `footprintBucket` field) and footprint as a dig target (`DigTargetInterval`). **Careful scope:** digging itself (1D → intraday candles etc.) stays; only the footprint leaf is removed. Follow the types: removing the kind will surface every dependent site via `tsc`. |
| Interval dropdown | Nothing to do beyond types — `chartIntervals` drives it. |
| Tests | Update `tests/chartRuntime.test.ts` for removed interval; add the `"footprint"→"1m"` migration assertion (`04` §10). Grep `footprint` (case-insensitive) across `apps/` — including `styles.css` classes — and clean up. |

## 5. Done criterion for this stage

`grep -ri footprint --include='*.py' --include='*.ts' --include='*.tsx' systems/ apps/` returns
nothing; repo-wide matches remain only in `bid_ask_vp_plan/` docs and historical notes under
`docs/` / `alpaca-data-pipeline-plan/` that describe the removal itself. All gates in `06` §6 pass.
