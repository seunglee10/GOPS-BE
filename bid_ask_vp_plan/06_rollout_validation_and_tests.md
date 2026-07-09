# 06 — Rollout order, validation & tests

## 1. Implementation order (single Goal-mode run, gated stages)

Each stage ends with the gate in §6 (scoped to what exists so far). Do not reorder stages 6→7:
footprint removal comes only after the replacement is proven.

| Stage | Work | Plan ref |
|---|---|---|
| 0 | Read `AGENTS.md`, `docs/CHART_DATA_REBUILD_PLAN.md`, `docs/STRUCTURE_GUIDE.md`, this plan set | — |
| 1 | `alfaka.orderflow` package (classification move-copy — footprint.py still present —, bins, quote cache, config) + unit tests | `01` §1, §5 |
| 2 | Subscription `orderflow` source + controller pin + cap priority + env/config plumbing | `01` §4 |
| 3 | Streaming: processor wiring, Redis HASH writes, session-rollover DEL, throttled publish, `order_flow_event` dto | `01` §2–3, `03` §2.1 |
| 4 | Storage: DDL in both initdb copies (+ ensure-schema list), rollup module + job entrypoint + compose jobs service + AWS CronJob manifest + kustomization | `02` |
| 5 | api-server: 3 REST endpoints + provider/redis accessors + stream_hub delivery/backpressure edits + tests | `03` |
| 6 | Frontend: types/chart-type plumbing → `orderFlow.ts` + `orderFlowRender.ts` + `orderFlowClient.ts` → View A → View B panel + registration → click linking → agent reference | `04` |
| 7 | Footprint removal (backend → frontend → config/docs), incl. `"footprint"→"1m"` migration | `05` |
| 8 | Docs sweep (`CHART_DATA_REBUILD_PLAN.md`, `ENVIRONMENT.md`, `cdc.md`, `about_front.md`, `platform/clickhouse/README.md` table list, `platform/redis` key notes if a key inventory exists) + full validation run | §6 |

No Kafka topic changes anywhere — `platform/kafka/topics.txt` stays untouched (assert this in
review).

## 2. Test inventory (new/changed)

Backend (unittest, existing discovery paths):

| File | Covers |
|---|---|
| `systems/market-data/tests/test_orderflow_classification.py` | `classify_trade_side` cases (port of test_footprint scenario), streaming as-of merge, window quote-carry |
| `systems/market-data/tests/test_orderflow_bins.py` | side accumulation, pin/session gating, sessionDate/ET edges, minute rollover, Redis hash field format, session-rollover DEL, publish throttle + minute-flush |
| `systems/market-data/tests/test_orderflow_subscription.py` | cohort source reconcile, layers, cap-survival priority |
| `systems/market-data/tests/test_orderflow_rollup.py` | rollup rows vs hand-computed fixture, hourly quote carry, closed date, dry-run, re-run idempotency, DDL presence in both initdb copies |
| `systems/api-server/tests/test_order_flow_query.py` | 3 endpoints (ready/empty/unsupported), FINAL query, stream_hub delivery matrix + droppable backpressure |
| updated: `test_chart_derived_data_worker.py`, `test_market_data_query.py`, `test_market_data_hardening.py` | footprint removal fallout (`05`) |

Frontend (`scripts/run-chart-tests.mjs` suite): `04` §10 list (bidask command validation, ladder
utils fixtures, minute-replace idempotency, footprint→1m migration).

## 3. Side-classification accuracy verification (TO-VERIFY #2)

- Unit level: the fixture in `test_orderflow_classification.py` pins exact expected splits.
- Empirical level: `scripts/local/verify_orderflow_classification.py` (new, dev-run, not CI):
  runs the rollup aggregation for one pinned symbol-day and prints
  `{tradeCount, quoteCount, askShare, bidShare, unknownShare, firstHourUnknownShare}`.
  Acceptance heuristics to print against (warn, not fail): overall `unknownShare < 5%`;
  `firstHourUnknownShare` may be higher (quote warmup) — if `> 15%`, investigate NBBO staleness
  (quote feed gaps) before launch. Document findings in the PR description.
- Live-vs-EOD drift: after one live session, compare the Redis session sum vs the EOD table for the
  same day (`scripts/local/verify_orderflow_live_vs_eod.py`, prints per-side relative diff; expect
  small drift from the quote-cache staleness — record the number; if per-side drift > ~10%,
  revisit `ORDER_FLOW_QUOTE_REFRESH_MS`).

## 4. WS load verification (TO-VERIFY #3 / handoff §6)

`scripts/local/orderflow_ws_load.py` (new, dev-run): opens N concurrent `/ws/charts?symbol=NVDA&interval=1m`
clients (default N=50) against a running local stack while a replay/producer pushes trades;
measures events/sec per client, inter-event gap, and counts `ERROR` overflow closes.
Acceptance: with the 250ms throttle, event rate per symbol ≤ ~4/s regardless of trade rate; 50
clients sustain 5 pinned symbols with zero overflow errors. Also note subscriber-side cost:
`_broadcast_latest_redis_live_events` polling is unchanged; the only new load is pub/sub fan-out.

## 5. `market_session` verification (TO-VERIFY #1)

- The rollup logs the in-window `market_session` distribution per symbol-day (`02` §3.2) — this is
  the standing verification.
- One-time check before launch (run manually, paste into PR):

```sql
SELECT market_session, count() AS n
FROM market_data.trade_ticks
WHERE symbol IN ('NVDA','AMZN','MU','AAPL','GOOGL')
  AND event_time >= now() - INTERVAL 7 DAY
GROUP BY market_session ORDER BY n DESC;
```

  Expected: overwhelmingly `regular/pre/after/overnight`, near-zero `unknown`. If `unknown` is
  material, fix at ingest (`build_raw_envelope` in `alfaka/common/market_messages.py`) before
  relying on the column; the rollup's time-window primary filter keeps the feature correct
  meanwhile.

## 6. Full validation gate (must all pass at the end; subsets per stage)

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend \
  python -m compileall -q systems
PYTHONPATH=... python -m unittest discover systems/market-data/tests
PYTHONPATH=... python -m unittest discover systems/api-server/tests
PYTHONPATH=... python -m pytest systems/order/tests/kis_trader
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend           # includes tsc -b
docker compose config --quiet
kubectl kustomize infra/k8s/base >/tmp/gops-k8s-base.yaml
kubectl kustomize infra/k8s/overlays/aws >/tmp/gops-k8s-aws.yaml
git diff --check
```

Runtime smoke (local stack + `--profile alpaca` during US regular hours, or replayed data):

```sh
curl -fsS 'http://localhost:8000/api/charts/order-flow/symbols'
curl -fsS 'http://localhost:8000/api/charts/order-flow/intraday?symbol=NVDA'
curl -fsS 'http://localhost:8000/api/charts/order-flow/daily?symbol=NVDA&from=2026-07-01&to=2026-07-09'
curl -fsS 'http://localhost:8000/api/charts/footprint?symbol=NVDA&from=...' # MUST be 404 after stage 7
docker compose --profile jobs run --rm order-flow-daily-rollup --date 2026-07-08 --dry-run
```

UI smoke: chart panel → chart type `Bid/Ask` (interval locks 1D, columns render, today fills live);
insert 오더플로우 panel (symbol select, slider, live minute updates, L1 quote); click a 1D bar on a
same-symbol chart → panel shows that date, deselect → back to live; click a bidask column → agent
chip appears and `/api/agents/analyze` payload carries a `chart.orderFlow` reference; non-pinned
symbol → both views show the unsupported state; VP overlay, indicators, compare, drawings, orders —
all unchanged.

## 7. Deploy / migration notes (AWS)

Ordered runbook for the operator (include in PR description):

1. **ClickHouse DDL first** (before any pod rollout): apply the `CREATE TABLE ...
   order_flow_profile_daily` from initdb to the live cluster manually (additive-only, per
   `docs/EKS_DATA_PRESERVING_REBUILD_PLAN.md` policy). Verify with `EXISTS TABLE`.
2. Config: apply configmap changes (`ORDER_FLOW_*` added, `CHART_FOOTPRINT_*` removed).
3. Roll pods in order: `subscription-controller` (pin starts) → `market-processor` (live bins
   start) → `gops-backend` (routes/WS) → `gops-frontend`. The old frontend against the new backend
   only loses the footprint endpoint at the final step — deploy frontend promptly after backend.
4. Add the CronJob (`kubectl apply` via overlay) — first scheduled run is the next weekday 21:30
   UTC; or trigger once manually (`kubectl create job --from=cronjob/...`).
5. Optional seed: run the rollup once with `--backfill-days 10` (`02` §3.4).
6. Watch: processor logs (order-flow write/publish counters — add INFO counters every N events),
   `subscription:symbols` contains the 5 pins, Redis `order-flow:*:live` keys populate during
   regular hours, WS clients receive `ORDER_FLOW_BINS_UPDATE`, first EOD run inserts rows.
7. Rollback: revert pods; the new table and Redis keys are inert for old code; re-adding footprint
   requires reverting the removal commit (stage 7 is intentionally the last commit-group).

## 8. Success criteria recap

1. Confirmed DDL live in both initdb copies + AWS cluster; EOD rows appear per session for 5
   symbols; re-runs idempotent.
2. Live path: pinned symbols' Redis HASH fills during regular session only; WS events throttled,
   minute-replace semantics verified.
3. REST/WS contracts exactly as `03` (payload field names are part of the contract).
4. View A + View B behave per `04` incl. the click-link rule and agent reference.
5. Footprint fully removed per `05` §5 grep criterion; every preserved feature (candles, VP
   overlay, indicators, compare, orders, agent flows) passes the existing test suites unchanged.
6. All §6 gates green.
