# Milestone 1 Live Path

Date: 2026-06-30
Status: local/runtime contract gate passed; EKS trace intentionally deferred by user instruction

## Scope Completed

- Promoted the Python stream processor from an unrendered example manifest to an explicit Kubernetes deployment: `infra/k8s/base/deployment-market-processor.yaml`.
- Included `alfaka-market-processor` in base and AWS kustomize output using the existing `gops-market-processor` image.
- Added `KAFKA_PROCESSOR_GROUP_ID` as the preferred processor group env while preserving `KAFKA_FLINK_GROUP_ID` as legacy fallback.
- Added shared runtime config validation so ingestor, processor, S3 sink, and ClickHouse loader fail fast on empty values or `YOUR_` / `REPLACE_` placeholders.
- Added ClickHouse loader support for manual commit mode and set `KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT=false` in compose/k8s/env docs.
- Extracted processor raw-envelope handling for isolated smoke tests without writing fake candles into local runtime.
- Added read-only live-path trace helpers:
  - local: `scripts/local/check-live-path.py`
  - AWS/EKS: `scripts/aws/check-live-path.sh`
- Added a short live-path trace runbook to `systems/market-data/README.md`.

## Checks Passed

- `python -m unittest discover systems/market-data/tests`: 61 tests passed.
- `python -m unittest discover systems/api-server/tests`: 29 tests passed.
- `python -m compileall -q systems`: passed.
- `kubectl kustomize infra/k8s/base`: passed and renders `alfaka-market-processor`.
- `kubectl kustomize infra/k8s/overlays/aws`: passed and renders the ECR-backed `alfaka-market-processor`.
- `docker compose config --quiet`: passed.
- `git diff --check`: passed.

## Local Runtime Smoke

- Docker containers for backend, frontend, Kafka, Redis, ClickHouse, processor, S3 sink, ClickHouse loader, backfill worker, and order workers are running.
- `GET /health` returns `{"status":"ok","service":"gops-backend"}`.
- `GET /api/charts/candles?symbol=NVDA&interval=1m&limit=3` returns real stored candle data.
- Local processor logs show subscription to all expected raw topics.
- Local Kafka consumer group `alfaka-local-stream-processor` shows lag `0` across raw topics.
- `scripts/local/check-live-path.py NVDA --interval 1m` reports API `ok`, Kafka `ok`, and Redis `warn` because no current live/recent Redis keys are present for NVDA in this local closed-stream state.
- `scripts/aws/check-live-path.sh NVDA` now reaches `kubectl` correctly; the remaining failure is cluster access, not the trace wrapper.

## Browser Smoke

Opened `http://localhost:5173` in the in-app browser.

- Page title: `GOPS Layout Runtime`.
- Chart canvas is present and nonzero sized.
- Watchlist/chart text renders with current stored NVDA data.
- No browser console warning/error was captured.
- `Hot Ranking` is still absent, as expected before Milestone 2.

## Remaining Gate

- AWS CLI now authenticates as `arn:aws:iam::<aws-account-id>:user/heejun`.
- EKS cluster discovery succeeds for `gops-eks-cluster` in `ap-northeast-2`, and local kubeconfig was generated for that cluster.
- Kubernetes API access still fails because `user/heejun` is not an EKS access entry principal.
- Existing EKS access entries include `arn:aws:iam::<aws-account-id>:user/boom` and `arn:aws:iam::<aws-account-id>:role/gops-github-actions-dev-deploy-role`, both associated with `AmazonEKSClusterAdminPolicy`.
- Updating kubeconfig to use `gops-github-actions-dev-deploy-role` also fails because `user/heejun` is not authorized for `sts:AssumeRole` on that role.
- The user then instructed not to use EKS and to continue by assuming the AWS deployment shape.
- Current local implementation should proceed without further EKS attempts. Treat `Alpaca -> raw Kafka -> Python processor -> Redis/processed Kafka -> ClickHouse/API/WebSocket -> browser` in AWS as out-of-band and unverified until the user reopens AWS verification.
- If AWS verification is reopened later, use controlled replay outside market hours and market-hours smoke when available.

## Required External Access To Resume

- No external access is required to continue local milestones under the AWS deployment assumption.
- Only if AWS verification is reopened:
- Use an AWS identity that is already an EKS access entry, such as `user/boom`, or grant `user/heejun` EKS access explicitly.
- Alternative: allow `user/heejun` to assume `arn:aws:iam::<aws-account-id>:role/gops-github-actions-dev-deploy-role` and keep kubeconfig configured with that role ARN.
- After access is corrected, verify `kubectl get pods -n alfaka-market-data` before running the trace.
- Rerun `scripts/aws/check-live-path.sh NVDA` during market hours, or run controlled replay plus keep market-hours smoke as a final closure gate.
