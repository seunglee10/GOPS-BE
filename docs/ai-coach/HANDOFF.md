# AI Coach Handoff

## Purpose and user questions

The AI coach evaluates a decision process separately from its profit or loss. It answers
what was traded, which checks were recorded or missed, which prior point-in-time cases
were similar, how the portfolio changed, and which position-specific conditions deserve
observation. Profit alone is never a good-process grade and loss alone is never a
bad-process grade.

## Four-page screen

The existing workspace hosts one `AI 투자 코치` panel with four internal pages.

1. `당일 거래 회고`: fill switching, a concise decision sentence, current and up to six
   similar cases, missed-check markers, portfolio impact, and a compact condition preview.
2. `판단 습관과 다음 원칙`: independent `진입`, `청산`, and `포트폴리오` tabs for
   `30d`, `90d`, and `1y`. Each tab uses its own sample, metrics, confidence, and
   missing-data state.
3. `효과·보완 조건`: deterministic priorities, candidate experiments, and guardrails.
4. `실행·알람 관리`: one action center combining the former pages 4, 5, and 6:
   experiments, guardrails, recommended alerts, and already watched alerts.

All coach alert creation and full sell/watch condition detail are centralized on page 4.
Page 1 renders exactly one sell/watch condition at a time, with the condition label and
current-to-threshold summary. Side arrows move between conditions and stop at the first and
last item. Selecting the active preview only moves to page 4 and focuses the matching
candidate, and never calls `POST /api/alerts`. A selected similar case likewise renders one
of `그때의 실수`, `오늘과 같은 점`, or `오늘과 다른 점` at a time with clamped side-arrow
navigation. Daily-trade page-4 candidates preserve `currentValue`,
`threshold`, `operator`, `detail`, `recommendedAction`, and `alertSupported` from the
deterministic condition. Recommended candidates are grouped by the
deterministic source `daily_trade`, `entry_habit`, `exit_habit`, or `portfolio_risk`,
rendered respectively as `당일 거래에서 제안`, `진입 습관에서 제안`,
`청산 습관에서 제안`, and `포트폴리오 위험에서 제안`. An empty source group stays
empty instead of receiving a fabricated candidate. RSI, relative-volume, and portfolio
conditions remain visible but do not receive a fake price-alert request.

Page 1 has no visible trade-symbol tab row or carousel counters. Today's fills switch with
arrows around the active company summary, and current/similar cases switch with arrows beside
the chart. These controls float over the content edge instead of reserving side columns, and
disabled end controls are hidden. The decision summary is a single prominent neutral sentence
without a repeated grade heading. `차트`, `뉴스`, `재무`, and `시장` render as a readable
two-by-two overview with category headings, text status badges, and up to two prioritized item
labels. Small evidence values are omitted from the default view; full evidence, source, and
as-of values remain available on hover or keyboard focus.

The page-1 chart uses a compact 360-pixel plotting viewport on a shared daily `T-60..T+20`
axis. It renders normalized OHLC plus volume, RSI, and MACD. Entry and missed
price/volume/RSI/MACD observations are anchored
at their relative day with a marker and dashed guide. Today's path stops at the latest
available observation; no future candle or forecast is drawn. The supplied prototype is
preserved under `reference/` and is never imported by production code.

## Trusted data and one snapshot

The browser submits only a small `coachRequest`. The API strips any client-supplied
`coachInputSnapshot`. After Kafka consumption, `agent-analysis-worker` identifies the
session owner from the signed envelope and builds exactly one `coach-input.v1` from:

- PostgreSQL: user-owned canonical KIS order fill state, filled paper orders, fill-scoped
  portfolio before/after history, decision-check events, and existing alerts;
- Redis/ClickHouse: cutoff-safe current quote, verified daily candles, company metadata,
  point-in-time news, SEC facts/derived metrics, and stored Yahoo earnings dates;
- GraphDB: current ontology evidence, explicitly marked current-only and ineligible for
  historical similarity;
- existing request metadata and per-source `sourceAsOf` values.

The snapshot has request/user, fills, positions and portfolio before/after,
market/chart/indicator/news/fundamentals/earnings/ontology sections, `sourceAsOf`, and
`missingData`. Role code receives this single object and does not independently refetch
coach inputs. The PostgreSQL reads run in one read-only repeatable-read transaction, and
the user subject is stored as a hash rather than the raw session identifier.

`StoreCoachPointInTimeContextProvider` is called once by Snapshot Builder for the current
and historical fill set. It never calls SEC, Yahoo, or Alpaca external APIs on the request
path. Redis current trade is accepted only when separate received/inserted time proves
availability by the request cutoff, it is at or after the fill, and it is within the
freshness window; the default maximum age is 5,760 minutes (96 hours). The current
event-time-only Redis row therefore falls through to deterministically ordered ClickHouse
trade ticks and closed 1-minute candles as bounded
fallbacks. Older values remain `missingData` rather than being labeled current. News
uses published/received/inserted availability, and SEC rows use filing/revision/computation/
insertion bounds. Because current SEC serving rows preserve filing date rather than exact
acceptance time, a same-trading-day filing is excluded from an intraday entry snapshot.

Decision and performance time are separate. `decisionAt` is derived from the server-owned
order/check time and bounds evidence and similarity features. `filledAt` anchors the executed
entry and outcome window. Daily similarity features select the canonical ClickHouse candle
revision with `inserted_at <= decisionAt`; display and outcome series may use revisions only
up to the immutable request cutoff. Providers without revision-aware primitives cannot
supply similarity features.

Yahoo earnings rows are accepted only when collected and inserted by the request cutoff,
but the current `ReplacingMergeTree` does not preserve a provable historical consensus
revision. Reports therefore set `historicalRevisionAvailable=false` and do not feed those
rows into historical similarity. GraphDB has no historical graph contract, so ontology is
`temporalScope="current-only"`, `historicalSimilarityEligible=false`, and has no invented
`sourceAsOf`. Any unavailable or ineligible source remains in `missingData` instead of
being replaced with a current value.

Both KIS and paper fills are normalized into the same snapshot without sharing identities.
KIS reconciliation stores point-in-time canonical cumulative state in append-only
`order_coach_fill_history`, with stable `kis:{orderId}` identity across partial/final replay.
Each strictly increasing positive cumulative quantity records `user_sub`, `order_id`,
`fill_id`, `first_filled_at`, `cumulative_filled_qty`, payload, and repository `observed_at`
transactionally. Equal/lower replay does not append, and delayed analysis selects only a
row observed by its request cutoff. `orders.coach_filled_at`/`coach_fill_payload` remain the
latest-state compatibility projection. A positive cumulative quantity on a canceled final
row is still an actual partial fill and remains in history.
The `executions` table remains an audit log and is not read as a coach fill ledger. Paper IDs
are `paper:{orderId}`. At actual paper fill, the matcher records the exact
before/after pair in the fill transaction with `valuationBasis="cost_basis"`, cash balance,
quantity, average price, and `costBasisValue`. These figures compare paper acquisition cost
and cash only; the UI labels them as such and never presents them as market valuation.
Portfolio impact for either mode requires an exact `fillId` + `phase=before|after` pair;
otherwise it remains `계산되지 않음` and never borrows an adjacent account snapshot.

Order ticket and quick-order requests may carry `decision-checks.v1`. The current client
always sends the six visible keys (`chart.rsi`, `chart.macd`, `chart.volume`,
`news.company`, `fundamentals.earnings`, `market.context`) as `checked` or `unchecked`.
The server rejects unknown/duplicate fields and client evidence/timestamps, assigns the
allowlisted label/category and capture time, and stores the normalized JSON on the order.
Only an actual KIS or paper fill materializes one event per key; the unique
`(user_sub, fill_id, check_key)` boundary makes retry idempotent. Canceled/rejected and
legacy orders without this input do not acquire inferred confirmation history. Events for
historical fills stay attached to those historical cases for case-specific process review.

Holdings polling always updates the latest portfolio observation, but appends history
only when the incoming JSONB payload differs after top-level `asOf`/`sourceAsOf`
observation timestamps are removed. Per-user PostgreSQL advisory transaction locks
suppress concurrent poll replays, while any changed position, cash, valuation, or
transaction state is retained as a new point-in-time row.

## Deterministic ownership and LLM boundary

`gops_agents.coach_analytics` owns:

- similarity components and centrally defined weights;
- entry-time feature availability, future-data rejection, self-exclusion, deterministic
  ordering, and the six-case maximum;
- MFE, MAE, post-entry return, and holding duration over the bounded outcome window;
- process grade independently from outcome P/L;
- portfolio before/after weights and concentration flags;
- page-2 period/stage aggregation, page-3 priority, and page-4 alert candidates;
- support/resistance, relative-volume, and RSI observation conditions.

Exit-habit generation uses real sell cases only. A pre-sale giveback observation uses
completed `T-60..T-1` highs and requires the configured minimum sample/recurrence threshold.
A post-sale observation requires the full `T+1..T+20` high path as of the immutable request
cutoff and is labeled hindsight MFE. It is a split-exit comparison candidate, not proof that
the original exit was a mistake. Without an explicit plan-confirmation record the report
continues to say plan consistency is unavailable.

The LLM may rewrite already computed findings into clear prose. It must not calculate or
replace scores, returns, thresholds, weights, sample size, confidence, or priority.

## Report and AWS flow

`AnalysisReport.coachReport` is optional and versioned as `coach-report.v2`:

```text
contractVersion, analysisId, generatedAt, sourceAsOf
page1, page2, page3, page4
snapshotRef, snapshotDigest, missingData, warnings
```

Page 1 includes `reviewsByFillId`; selecting a fill swaps its chart, markers, similar
cases, assessment, checklist, portfolio impact, and conditions atomically. Page 2 stores
real `reportsByPeriod`, not one report relabelled by the UI. Page 4 is the single merged
action center.

Selecting a historical case on page 1 also selects that case's `TradeCase.checklist`.
Seeded replay cases expose unchecked price, momentum, and volume checks from the same stored
daily-candle window; categories without an archived decision record say `확인 기록 없음`.

The preserved runtime path is:

```text
POST /api/agents/analyze -> 202 analysisId
-> Kafka agents.analysis-requests.v1
-> agent-analysis-worker -> trusted Snapshot Builder
-> optional immutable S3 archive -> deterministic coach analytics
-> AnalysisReport.coachReport -> Redis
-> agents.analysis-results.v1 -> delivery gateway
-> polling/SSE -> AI coach panel props
```

AWS overlays explicitly use Kafka and Redis backends; they do not allow `auto` memory
fallback. `AGENT_OUTPUT_KAFKA_REQUIRED=true` makes result-publish failure visible instead
of treating delivery as complete. The worker writes an enabled archive to the dedicated
private, versioned, encrypted bucket with `If-None-Match: *`; the report exposes only the
object key and its verified SHA-256 digest. Its IRSA can put and get objects only under
the coach prefix, without list or delete. A 412 retry cannot overwrite the object: the
worker reads, verifies, and reuses the first immutable input as
`already_exists_reused`. AWS overlays set archive `REQUIRED=true`, so an
archive failure fails the coach request rather than producing an unaudited report. Every
agent rollout runs `scripts/aws/verify-ai-coach-snapshot-s3.sh` inside the deployed worker;
its non-sensitive conditional-put and digest-read canary verifies both live IRSA paths.
Apply the Terraform `GetObject` policy before rolling out this worker image.

The versioned bucket does not retain a second 90-day copy. Current snapshots expire
after `ai_coach_snapshot_retention_days` (default 90), and the resulting noncurrent
version becomes eligible for permanent deletion after
`ai_coach_snapshot_noncurrent_retention_days` (default 1, constrained to 1-7). Thus the
default content-retention eligibility window is about 91 days; S3 lifecycle execution
itself is asynchronous.

The current archive-first coach does not query the order schema at panel-open time or while
building its post-market input. `0008_alert_proposal_source.sql` is nevertheless required
before rolling out page-4 alert creation because the API persists the selected page origin in
`alerts.proposal_source`. The older `0006`/`0007`/`0009` coach migrations remain compatible
order-domain history support, not a runtime dependency of the S3 input/report path. Existing
alerts without the nullable field render as `출처 기록 없음`; no historic fill or decision
record is inferred or backfilled by the coach.

The agent image includes `sp500-heatmap-seed.json`; it is only a timestamped metadata
fallback and its `sourceRetrievedAt` must pass each fill cutoff. AWS overlays make
ClickHouse/OpenAI Secrets mandatory for the agent runtimes and the order database Secret
mandatory for the analysis worker. Fixture data is not an AWS fallback.

## Missing data and development fixture

Null values remain null and render as `데이터 부족`, `표본 부족`, `확인 기록 없음`,
`계산되지 않음`, `일정 확인 불가`, `유사 사례 부족`, or `데이터 연결 대기`. The
engine never fabricates market values.

The fixed UI report activates in development when both `import.meta.env.DEV` and
`VITE_AI_COACH_DEV_FIXTURE=true`. The same dynamically imported report is also used for an
untouched `diversified-us-v3` paper account so its 10 holdings, 23 fills, 3 pending orders,
sector weights, and guardrails remain coherent instead of showing an older archived report.
Any current-generation order without `seed_profile` disables this seeded-account path. The
fixed report is never written to Redis, ClickHouse, Kafka, PostgreSQL, or S3.
Switching between LIVE and SIM does not clear or replace the resolved coach report. The panel keeps
the same report and internal page in both modes; simulator status is not an AI-coach availability
condition.
For the seeded report only, page-1 chart series are built from the repository's stored fixed-replay
AAPL/AMZN/WMT daily candles. The fill-relative view rebases OHLC prices to the ledger fill while
preserving actual returns and volumes, and computes RSI/MACD from those stored closes. Confirmation
items use a flat `DESIGN.md` to-do-list treatment with inline evidence and semantic typography roles;
the old nested cards, hover-only evidence, generated waves, fluid font sizes, and heavy local weights
are not part of the contract.
Page 2 follows the same presentation contract: semantic stage tabs, flat evidence and habit rows,
label/count/meter-only strength summaries, problem recommendations without secondary observed-behavior
copy, hairline-separated representative-trade sections, and no local typography metrics, decorative
shadows, gradients, or nested card hierarchy.

The AI coach production component is lazy-loaded as its own frontend chunk so the AWS
quality workflow's 512,000-byte JavaScript chunk budget remains enforced. The panel
layout serializer and legacy-layout restore path both remove `coachReport`; account trade,
portfolio, chart, and snapshot references therefore remain in authenticated runtime state
and are not persisted in the browser's shared layout `localStorage`.

## Validation and deployment acceptance

Local source validation:

```text
git diff --check
npm run test:ai-coach --prefix apps/gops-frontend
npm run test:layout --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
npm run test:bundle-size --prefix apps/gops-frontend
PYTHONPATH=systems/agent-orchestration/shared:systems/market-data/shared \
  .venv/bin/python -m pytest systems/agent-orchestration/tests
.venv/bin/python -m pytest systems/api-server/tests/test_order_routes.py \
  systems/api-server/tests/test_paper_trading_routes.py
.venv/bin/python -m pytest systems/order/tests/kis_trader/unit
kubectl kustomize infra/k8s/overlays/aws
kubectl kustomize infra/k8s/overlays/aws-incluster-app-ci
terraform -chdir=infra/aws/terraform fmt -check
terraform -chdir=infra/aws/terraform init -backend=false
terraform -chdir=infra/aws/terraform validate
```

For visual review, run Vite with `VITE_AI_COACH_DEV_FIXTURE=true`; do not start the
Alpaca realtime ingestor. Verify four pages, all three page-2 tabs, all periods, six-case
switching, keyboard tooltips, and narrow/wide resize.

Static tests, manifest rendering, and Docker builds do not prove a live AWS data path.
The deployment gate now proves a conditional encrypted S3 write from the real analysis
worker IRSA, but it does not synthesize a logged-in user session. Full staging acceptance
still requires Terraform apply, migration success, pod rollout, one
authenticated `202` request, Kafka consumption, a Redis completed report, polling/SSE,
an S3 object whose digest matches the report on its first write, a retry collision check
that preserves the original object, and a cross-user isolation check.

### Live AWS read-only audit (2026-07-14)

No deploy, push, or AWS mutation was performed during this task. A read-only check of the
configured dev account found `gops-eks-cluster` active and `agent-analysis-worker` healthy
with zero restarts. The worker uses `ai-coach-worker-sa`; its IRSA role, required archive
flags, bucket, and prefix match the Terraform contract. The snapshot bucket has versioning
and AES256 encryption enabled. Four small deploy-smoke objects prove worker-IRSA PUT access,
but no authenticated analysis snapshot has yet been observed. The deployed cluster also
lacks `alfaka-yahoo-estimates-sync`, and the order database has migrations only through
`0007`: `alerts.proposal_source` and `0008_alert_proposal_source.sql` are not deployed.
The live order database has filled paper orders but no execution rows; the current trusted
snapshot provider in the deployed, older image reads `orders` joined to `executions`, not
`paper_orders`, so those paper fills cannot yet populate the coach report. The
decision-check table also has no live rows.
Therefore the archive infrastructure is active, but this worktree's report/UI/migration and
collector changes are not live. Commit/push, migration, image rollout, and the authenticated
staging acceptance sequence above are still required before claiming end-to-end coach data.

## Conflicts and remaining dependencies

### Maintenance direction (2026-07-14)

The coach is maintained as a post-market reader, not an order-screen feature.  The
current implementation does not add purchase-time checkboxes, KIS reconciliation, paper
order persistence, or a decision-event writer.  Its server-owned input is one completed
S3 object per authenticated user and New York trading date:

```text
ai-coach/input/v1/user={sha256(userId)[:24]}/date=YYYY-MM-DD.json
```

That object must carry `sourceAsOf` (or `generatedAt`) plus any fills, historical fills,
portfolio pairs, optional recorded decision evidence, and prior alerts available to the
post-market job. Missing sections remain missing; absent decision evidence is rendered as
`확인 기록 없음`, not inferred as a user action. The worker reads this archive once,
uses ClickHouse only for cutoff-safe market/chart context, stores its immutable audit
snapshot, and writes the first daily `coach-report.v2` to
`ai-coach/reports/v1/user={hash}/date=YYYY-MM-DD/report.json`. A small `latest.json`
pointer lets the authenticated backend serve `GET /api/ai-coach/reports/latest` without
Redis, polling, or a new analysis request when the panel opens.

This is deliberately not a scheduler or user-data exporter. An upstream post-market
exporter must produce the input object before a report can exist. Until it does, the
production panel shows a clear waiting state rather than fixture data or invented values.
The deployed API service account needs `s3:GetObject` only for the report prefix; the
analysis-worker role needs input reads and snapshot/report writes. Apply the Terraform
policy change before rolling out the images.

### AWS read-only readiness gate (2026-07-14)

`bash scripts/aws/preflight-ai-coach-aws.sh` verifies the deployed archive flags, service
accounts, ClickHouse serving tables, alert-source migration, Yahoo CronJob, and the exact
worker/API `GetObject` permissions without writing an object or changing a table. It must
pass after Terraform/IAM and image rollout. The separate
`scripts/aws/verify-ai-coach-snapshot-s3.sh` intentionally writes a non-sensitive canary and
is the only post-rollout check that proves worker `PutObject`; run it only as an explicit
deployment operation.

- The worktree was rebased without local commits onto `origin/dev`
  `8e2bfc8e2b519a979ceb19c827364252b1d5c6e3`. The only textual conflict was in
  `AGENT_FRONTEND_INTEGRATION.md`; both the latest chart-derived profile contract
  and the AI coach contract were preserved. The original repository worktree was
  not modified.
- Latest dev's local AWS deploy path exposed a macOS Bash 3.2 failure in the
  service detector. The detector now uses ordered scalar lists instead of Bash
  4 associative arrays, and its agent selection still adds `order-worker` so all
  migrations precede the analysis-worker rollout. The default in-cluster app
  overlay also includes the Yahoo estimates CronJob that the legacy AWS overlay
  already carried.
- Latest `origin/dev` did not contain the original worktree's uncommitted AI coach UI.
  Compatible page concepts were selectively ported; unrelated original changes were not
  reset, restored, or copied.
- The older docs and prototype described six pages and a short intraday line. The user's
  later instruction supersedes this with four pages, merging former pages 4/5/6, and a
  daily `T-60..T+20` chart.
- The snapshot provider now reads stored market, news, SEC, Yahoo, metadata, and GraphDB
  sources under explicit cutoff rules. A working code path does not guarantee that AWS serving
  tables contain rows for every symbol/time; gaps correctly remain `missingData` and must be
  verified with an authenticated staging analysis after rollout.
- GraphDB still has no historical ontology contract, and Yahoo's current estimates table does
  not preserve provable historical revisions. These dimensions remain excluded from historical
  similarity instead of being reconstructed from current data.
- Only orders created with `decision-checks.v1` can produce trusted decision events, and only
  fills processed by writers that record an exact `fillId`/phase pair can produce portfolio
  impact. The updated paper matcher records a cost-basis pair; no KIS pair is inferred from
  adjacent account observations. Existing history remains partial, and no migration invents
  what the user checked or what an old account looked like before a fill.
- Deterministic exit-habit rules now exist, but they require enough real sell samples and, for
  post-sale MFE, a complete `T+20` outcome window. An empty `exit_habit` group is therefore a
  valid insufficient-sample result, not a fixture fallback.
- Page-3 experiment/guardrail persistence needs an owning write API before toggles can be
  treated as durable across sessions. Alerts are created only after an explicit user
  action; no automatic order, liquidation, or alert activation exists.
- `proposal_source` is display metadata supplied with an explicit page-4 create request;
  it is not a cryptographically trusted audit assertion. Trusted provenance would need a
  server-owned proposal identifier and is outside this contract.
- Live AWS E2E was not established by source inspection. It must not be claimed until the
  staging acceptance evidence above is collected.
