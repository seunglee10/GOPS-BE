# GOPS Alfaka Market Data

GOPS chart UI가 Alpaca market data를 REST snapshot과 WebSocket stream으로 읽을 수 있게 하는 로컬/운영 공통 코드베이스입니다.

현재 기준 원칙은 단순합니다.

- Alpaca historical backfill은 실제 Alpaca Historical REST를 호출한다.
- 기본 Docker/운영 경로에서 더미 candle을 만들지 않는다.
- 과거 candle 초기 로딩은 `GET /api/charts/candles`가 담당한다.
- WebSocket `/ws/charts`는 live update, reconnect gap-fill, control event 전용이다.
- Chart API는 S3를 직접 읽지 않는다. Redis와 ClickHouse만 serving source로 쓴다.
- S3는 raw archive와 processed/final replay source이고, ClickHouse `chart_candles`는 serving projection이다.
- 로컬 smoke script도 실제 Alpaca historical data를 사용한다. credentials가 없으면 candle을 생성하지 않고 실패한다.

## Repository Map

```text
apps/chart-engine/                 chart document/runtime/canvas engine
apps/gops-frontend/                GOPS React frontend
packages/alfaka/alpaca/            Alpaca websocket/assets/subscription helpers
packages/alfaka/backfill/          historical backfill queue/status/runner/worker
packages/alfaka/common/            env, Kafka, Redis key, S3, secret helpers
packages/alfaka/serving/           Redis + ClickHouse provider and DTOs
packages/alfaka/storage/           ClickHouse loader, S3 sink, S3 materializer
packages/alfaka/streaming/         raw -> processed transform/local processor
services/01-alpaca-connector/      Alpaca live ingestor entrypoint
services/03-flink-stream-processor/local_main.py
services/05-clickhouse-store/      processed topic -> ClickHouse entrypoint
services/06-s3-store/              processed topic -> S3 entrypoint
services/07-api-websocket/         GOPS backend REST/WebSocket service
infra/clickhouse/                  local ClickHouse schema
infra/docker/                      Dockerfiles
infra/k8s/                         Kubernetes base and AWS overlay
infra/aws/terraform/               ECR/S3/Secrets/IRSA foundation
scripts/local/                     local smoke and inspection scripts
scripts/aws/                       AWS image/topic/apply helpers
tests/                             Python contract/regression tests
```

## Data Flow

Live path:

```text
Alpaca WebSocket
  -> Kafka raw topics
  -> local processor or Flink-compatible processor
  -> Kafka processed topics
  -> Redis hot/recent cache
  -> ClickHouse chart_candles serving projection
  -> S3 processed/final/live artifacts
  -> GOPS REST/WebSocket
```

Historical/backfill path:

```text
POST /api/charts/backfill
  -> Redis queue/status/lock
  -> backfill-worker
  -> Alpaca Historical REST
  -> S3 raw archive
  -> normalized processed candle contract
  -> S3 processed/final
  -> S3 materializer
  -> ClickHouse chart_candles
  -> GET /api/charts/candles ready snapshot
```

Recovery/rematerialization path:

```text
S3 processed/final
  -> packages/alfaka/storage/s3_materializer.py
  -> ClickHouse chart_candles
  -> load_audit
```

## Local Setup

Create `.env` with Alpaca keys:

```sh
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
ALPACA_FEED=sip
HISTORICAL_FEED=sip
ALPACA_UNIVERSE=semiconductor-100
ALPACA_SYMBOLS=NVDA,AMD,AVGO,TSM,ASML,AMAT,MU
```

Start the core stack:

```sh
docker compose up -d --build
```

This starts Redis, Kafka, MinIO, ClickHouse, local processor, ClickHouse loader, S3 sink, GOPS backend, GOPS frontend, and the historical backfill worker.

Open:

```text
Frontend: http://localhost:5173
Backend:  http://localhost:8000/health
Candles:  http://localhost:8000/api/charts/candles?symbol=AAPL&interval=1m&limit=160
Symbols:  http://localhost:8000/api/charts/symbols
```

Start real Alpaca live ingestion:

```sh
docker compose --profile alpaca up -d --build alpaca-ingestor
```

The live ingestor is profile-gated so local UI/backend work does not automatically open Alpaca WebSocket sessions.

## Backfill Usage

The frontend automatically requests backfill when a selected symbol/interval exists in the watchlist or registry but has no Redis/ClickHouse candles.

Manual request:

```sh
curl -fsS \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","interval":"1m"}' \
  http://localhost:8000/api/charts/backfill
```

Status:

```sh
curl -fsS "http://localhost:8000/api/charts/backfill/status?symbol=AAPL&interval=1m"
```

Snapshot after completion:

```sh
curl -fsS "http://localhost:8000/api/charts/candles?symbol=AAPL&interval=1m&limit=160"
```

The local smoke script uses the real Alpaca historical path. If Alpaca credentials or upstream data are unavailable, it should fail or show an empty state instead of generating candles:

```sh
bash scripts/local/smoke-backfill-missing-data.sh INTC
```

## Main API Contract

`GET /api/charts/candles`

- Reads Redis recent candles first, then ClickHouse historical candles.
- Returns `dataStatus`, `backfillStatus`, `canBackfill`, `message`, and `coverage`.
- `coverage` is the detailed readiness diagnostic. Backfill job success alone must not mark data as ready unless stored candle coverage is sufficient.
- Does not start long-running backfill by itself.

`POST /api/charts/backfill`

- Validates symbol against the configured universe, symbol registry, Redis metadata, or ClickHouse symbols.
- Queues a Redis-backed backfill request.
- Uses Redis lock/status keys to deduplicate identical symbol/interval/range work.

`GET /api/charts/backfill/status`

- Returns current request status for REST polling.

`WS /ws/charts`

- Sends live candle events and reconnect gap-fill from Redis/ClickHouse.
- Does not bulk-load historical candles.

## Operational Notes

- `config/market-data-request.json` defines named market universes and the default semiconductor universe.
- `ALPACA_UNIVERSE` selects search/validation candidates. Currently `semiconductor-100` is the supported named universe.
- `ALPACA_SYMBOLS` selects always-on Alpaca subscriptions and the default frontend Watch List seed. It must be a CSV ticker list; universe names are not accepted.
- An empty `ALPACA_SYMBOLS` never auto-subscribes the whole universe.
- `infra/aws/msk/topics.txt` is the topic creation input used by `scripts/aws/create-msk-topics.sh`.
- Kubernetes base includes `alpaca-ingestor`, `s3-sink`, `clickhouse-loader`, `backfill-worker`, backend, frontend, and a symbol registry sync job.
- AWS overlay renders locally; actual `terraform apply`, ECR push, and `kubectl apply` should only be run by the deployment owner.
- Alpaca keys belong in `.env` locally and Secrets Manager/Kubernetes Secret in AWS.
- If a local Redis/ClickHouse/MinIO volume already contains rows from older development runs, reset those local volumes before validating data trust. The current runtime no longer marks or creates development-generated candles, so old rows cannot be reliably distinguished after materialization.

## Verification

Run these before sharing or deploying changes:

```sh
env PYTHONPATH=packages python -m compileall -q packages services/07-api-websocket/gops-backend/app tests
env PYTHONPATH=packages python -m unittest discover tests
env PYTHONPATH=packages:services/07-api-websocket/gops-backend python -m unittest discover services/07-api-websocket/gops-backend/tests
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
docker compose config --quiet
kubectl kustomize infra/k8s/base >/tmp/alfaka-k8s-base.yaml
kubectl kustomize infra/k8s/overlays/aws >/tmp/alfaka-k8s-aws.yaml
git diff --check
```

Runtime smoke:

```sh
docker compose --profile alpaca up -d --build alpaca-ingestor
bash scripts/local/smoke-backfill-missing-data.sh INTC
```

The local runtime no longer publishes generated market events. If a chart has no Alpaca-backed Redis/ClickHouse data, the UI must show loading, empty, or backfill status honestly instead of drawing generated candles.

## Removed Legacy Docs

Earlier Goal planning documents and redirect READMEs were removed to avoid conflicting instructions. This `README.md` is the team-facing project guide; code, tests, compose files, and Kubernetes manifests are the current source of truth.
