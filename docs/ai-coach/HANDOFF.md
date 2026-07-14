# AI Coach Handoff

## Purpose and user questions

The AI coach evaluates a decision process separately from its profit or loss. It answers
what was traded, which checks were recorded or missed, which prior point-in-time cases
were similar, how the portfolio changed, and which position-specific conditions deserve
observation. Profit alone is never a good-process grade and loss alone is never a
bad-process grade.

## Four-page screen

The existing workspace hosts one `AI 투자 코치` panel with four internal pages.

1. `당일 거래 회고`: fill switching, process/outcome split, current and up to six
   similar cases, missed-check markers, portfolio impact, and position conditions.
2. `판단 습관과 다음 원칙`: independent `진입`, `청산`, and `포트폴리오` tabs for
   `30d`, `90d`, and `1y`. Each tab uses its own sample, metrics, confidence, and
   missing-data state.
3. `효과·보완 조건`: deterministic priorities, candidate experiments, and guardrails.
4. `실행·알람 관리`: one action center combining the former pages 4, 5, and 6:
   experiments, guardrails, recommended alerts, and already watched alerts.

The page-1 chart uses a shared daily `T-60..T+20` axis. It renders normalized OHLC plus
volume, RSI, and MACD. Entry and missed price/volume/RSI/MACD observations are anchored
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
join/time index used by point-in-time history. Roll out backend/order writers that
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
configured dev account found the EKS cluster `gops-eks-cluster` `ACTIVE`, but the currently
deployed `agent-analysis-worker` still uses the previous `alfaka-market-data-sa` service
account and an earlier image. Its live ConfigMap still reports queue/report backends as
`auto`, has no coach archive flags, and the planned dedicated snapshot bucket returns
`HeadBucket 404`. Therefore the source and images are deployment-ready, but the new
snapshot path is not currently active in dev. Terraform apply, image push, migration,
rollout, the new IRSA canary, and the authenticated staging acceptance sequence above are
still required before claiming live snapshot persistence.

## Conflicts and remaining dependencies

- Latest `origin/dev` did not contain the original worktree's uncommitted AI coach UI.
  Compatible page concepts were selectively ported; unrelated original changes were not
  reset, restored, or copied.
- The older docs and prototype described six pages and a short intraday line. The user's
  later instruction supersedes this with four pages, merging former pages 4/5/6, and a
  daily `T-60..T+20` chart.
- Actual news evidence, earnings calendar, fundamentals, ontology, and decision-check
  capture producers remain external dependencies. Their absence is explicit no-data.
- Page-3 experiment/guardrail persistence needs an owning write API before toggles can be
  treated as durable across sessions. Alerts are created only after an explicit user
  action; no automatic order, liquidation, or alert activation exists.
- Live AWS E2E was not established by source inspection. It must not be claimed until the
  staging acceptance evidence above is collected.
