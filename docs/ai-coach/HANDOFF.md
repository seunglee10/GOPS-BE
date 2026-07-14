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

- PostgreSQL: user-owned fills/orders, change-only append portfolio history,
  decision-check events, and existing alerts;
- ClickHouse: verified daily candles through the request time;
- existing request metadata and per-source `sourceAsOf` values.

The snapshot has request/user, fills, positions and portfolio before/after,
market/chart/indicator/news/fundamentals/earnings/ontology sections, `sourceAsOf`, and
`missingData`. Role code receives this single object and does not independently refetch
coach inputs. The user subject is stored as a hash, not as the raw session identifier.

News, fundamentals, earnings, and ontology sections currently remain explicit no-data
unless an upstream producer adds their point-in-time values to the builder. They must not
be inferred from current or post-entry information.

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
object key and, after a successful first write, its SHA-256 digest. Its IRSA is put-only
for the coach prefix. A 412 retry cannot overwrite the object and is reported as
`already_exists_unverified` with a null digest; this avoids granting snapshot read access
or falsely asserting unverified metadata. AWS overlays set archive `REQUIRED=true`, so an
archive failure fails the coach request rather than producing an unaudited report. Every
agent rollout runs `scripts/aws/verify-ai-coach-snapshot-s3.sh` inside the deployed worker;
its non-sensitive conditional-put canary verifies the real IRSA/bucket write path without
granting object read or delete access.

The versioned bucket does not retain a second 90-day copy. Current snapshots expire
after `ai_coach_snapshot_retention_days` (default 90), and the resulting noncurrent
version becomes eligible for permanent deletion after
`ai_coach_snapshot_noncurrent_retention_days` (default 1, constrained to 1-7). Thus the
default content-retention eligibility window is about 91 days; S3 lifecycle execution
itself is asynchronous.

Before AWS rollout, the deploy entrypoints automatically apply pending order migrations
whenever `order-worker` or `agent-orchestrator` is selected. `0006_ai_coach.sql` adds
`orders.user_sub`, `user_portfolio_snapshot_history`, and
`trade_decision_check_events`; `0007_ai_coach_execution_index.sql` adds the execution
join/time index used by point-in-time history. `0008_alert_proposal_source.sql` adds the
nullable, constrained `alerts.proposal_source` used to preserve coach proposal origin
through create/list, the next trusted snapshot, and page-4 watched-alert rendering. The
migration must complete before the updated API and analysis worker roll out. Existing and
manually created alerts remain null and render as `출처 기록 없음`. Roll out backend/order writers that
populate ownership and history before expecting historical coach data. Existing rows
without a reliable owner are intentionally not backfilled by inference.

## Missing data and development fixture

Null values remain null and render as `데이터 부족`, `표본 부족`, `확인 기록 없음`,
`계산되지 않음`, `일정 확인 불가`, `유사 사례 부족`, or `데이터 연결 대기`. The
engine never fabricates market values.

The fixed UI fixture activates only when both `import.meta.env.DEV` and
`VITE_AI_COACH_DEV_FIXTURE=true`. It is loaded through a DEV-only dynamic import, carries
a visible `DEV FIXTURE` label, and is never written to Redis, ClickHouse, Kafka, PostgreSQL,
or S3. Production has no fixture fallback.

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
snapshot provider reads `orders` joined to `executions`, not `paper_orders`, so those paper
fills cannot yet populate the coach report. The decision-check table also has no live rows.
Therefore the archive infrastructure is active, but this worktree's report/UI/migration and
collector changes are not live. Commit/push, migration, image rollout, and the authenticated
staging acceptance sequence above are still required before claiming end-to-end coach data.

## Conflicts and remaining dependencies

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
- Actual news evidence, earnings calendar, fundamentals, ontology, and decision-check
  capture producers remain external dependencies. The current snapshot builder deliberately
  reports those providers as not connected rather than reading populated stores without a
  point-in-time contract. Their absence is explicit no-data.
- Paper-trading fills are stored separately from `orders`/`executions` and are not currently
  selected by the trusted snapshot provider. Supporting them requires an explicit execution-mode
  contract plus matching paper-position history; silently mixing paper and live fills would make
  portfolio-before/after and similarity results unreliable.
- The production repository has no writer for `trade_decision_check_events`. Until a trusted
  capture path writes those events, process review and missed-check markers correctly remain
  `확인 기록 없음` for real analyses. Current quote, company, and sector enrichment also needs
  a point-in-time Redis/symbol-registry provider before current return and sector impact can be
  complete.
- The deterministic source mapping supports an `exit_habit` proposal, but the current raw habit
  engine does not yet derive an exit insight from trade history. Fixture and injected-contract
  tests can render the group; production data will leave it empty until that rule is implemented.
- Page-3 experiment/guardrail persistence needs an owning write API before toggles can be
  treated as durable across sessions. Alerts are created only after an explicit user
  action; no automatic order, liquidation, or alert activation exists.
- `proposal_source` is display metadata supplied with an explicit page-4 create request;
  it is not a cryptographically trusted audit assertion. Trusted provenance would need a
  server-owned proposal identifier and is outside this contract.
- Live AWS E2E was not established by source inspection. It must not be claimed until the
  staging acceptance evidence above is collected.
