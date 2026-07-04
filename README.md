# GOPS

GOPS is a real-time market-data, chart, and order-control platform.

Product direction: **종목을 찾는 사람에게 기준을, 시장을 읽는 사람에게 방향을.**
See `docs/AGENT_ARCHITECTURE.md` for the current agent direction and handoff boundaries. Future-facing product ideas are context, not implemented guarantees.

## Current Scope

The repository currently includes:

- React frontend and shared chart engine.
- FastAPI chart/order/WebSocket API server.
- Alpaca market-data ingest and on-demand historical fill.
- Kafka-compatible stream processing.
- Redis, ClickHouse, and S3 market-data serving/storage.
- KIS demo order API, Postgres persistence, outbox, broker adapter, migrations, and reconciliation.
- Agent-orchestration v1 with role-agent skeletons, market-event detection, and notification publishing.
- Local Docker Compose and early AWS/EKS deployment assets.

## Read First

| File | Use |
| --- | --- |
| `docs/README.md` | Index for agent handoff docs. |
| `docs/AGENT_ARCHITECTURE.md` | Agent runtime, provider boundary, snapshots, synthesis, and report contracts. |
| `docs/AGENT_BACKEND_INTEGRATION.md` | Agent API, idempotency, Kafka async path, Redis report store, polling, SSE, and alert WebSocket contracts. |
| `docs/AGENT_FRONTEND_INTEGRATION.md` | Agent chat submit, `analysisId`, report rendering, and layout/chart proposal handling. |
| `docs/AGENT_AWS_BUILD.md` | Agent image, EKS resources, Kafka, Redis/Valkey, ClickHouse, GraphDB, S3, secrets, and smoke checks. |
| `AGENTS.md` | Rules for Codex and future contributors. |

## Repository Map

```text
apps/gops-frontend/                React frontend
apps/chart-engine/                 chart document/runtime/canvas engine

systems/api-server/                FastAPI chart/order/WebSocket gateway
systems/market-data/               config, ingest, processing, storage, serving helpers, on-demand fill
systems/order/                     KIS demo order domain, outbox, adapter, jobs
systems/agent-orchestration/       role agents, event detector, notification publisher

platform/kafka/topics.txt          market/order Kafka topic contract
platform/*/README.md               local -> pod -> managed-service transition notes

infra/docker/                      Dockerfiles
infra/k8s/                         Kubernetes base and AWS overlay
infra/aws/terraform/               ECR/S3/Secrets/IRSA foundation
infra/clickhouse/initdb/           local ClickHouse schema

scripts/local/                     local smoke and inspection scripts
scripts/aws/                       AWS image/topic/apply helpers
shared/chart-contract/             cross-system chart command contract notes
docs/                              project reference docs
```

## Runtime Flow

```mermaid
flowchart LR
  FE["gops-frontend"] --> API["api-server"]
  API --> Redis["Redis"]
  API --> CH["ClickHouse"]
  API --> PG["Postgres"]
  API --> AgentOrch["agent-orchestrator"]

  Alpaca["Alpaca"] --> Ingestor["market-ingestor"]
  Ingestor --> Kafka["Kafka"]
  Kafka --> Processor["market-processor"]
  Kafka --> EventDetector["agent-event-detector"]
  EventDetector --> Kafka
  Kafka --> AlertPublisher["agent-notification-publisher"] --> Redis
  Processor --> Redis
  Processor --> S3Sink["s3-sink"] --> S3["S3"]
  Processor --> CHLoader["clickhouse-loader"] --> CH

  API --> PG
  PG --> Outbox["order-outbox"]
  Outbox --> Kafka
  Kafka --> KISAdapter["kis-adapter"]
  KISAdapter --> KIS["KIS demo API"]
  KISAdapter --> PG
  Reconciler["order reconciler job"] --> PG
  Reconciler --> KIS
```

## Local Setup

Create `.env` from `.env.example`.

Use one official local Python environment at the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python --version
```

The expected local Python version is `3.12.x`. Do not create duplicate project virtualenvs under `/tmp` or other ad hoc paths.

For AWS-backed local work, leave `S3_ENDPOINT_URL` and `DOCKER_S3_ENDPOINT_URL` empty and use:

```text
ALPACA_SECRET_NAME=dev/alpaca
S3_BUCKET=gops-market-data-<aws-account-id>-ap-northeast-2-an
AWS_REGION=ap-northeast-2
AWS_ACCESS_KEY_ID=<local restricted key if needed>
AWS_SECRET_ACCESS_KEY=<local restricted secret if needed>
AWS_SESSION_TOKEN=
```

Start the local stack:

```sh
docker compose --env-file .env up -d --build
```

Open:

```text
Frontend: http://localhost:5173
Backend:  http://localhost:8000/health
Agents:   http://localhost:8100/health
Symbols:  http://localhost:8000/api/charts/symbols
Candles:  http://localhost:8000/api/charts/candles?symbol=AAPL&interval=1m&limit=160
```

Start live Alpaca ingestion only when needed:

```sh
docker compose --profile alpaca up -d --build alpaca-ingestor
```

The live ingestor is profile-gated so normal UI/backend work does not automatically open Alpaca WebSocket sessions.

## API Contract

Chart API:

```text
GET  /api/charts/candles
GET  /api/charts/symbols
WS   /ws/charts
```

Deprecated chart backfill queue routes return `410 Gone`. `GET /api/charts/candles`
is the single chart read/fill entrypoint and includes a `fill` trace when data is
missing or partially filled.

Agent API:

```text
POST /api/agents/analyze
GET  /api/agents/reports/{analysis_id}
GET  /api/agents/reports/{analysis_id}/stream
WS   /ws/agent-alerts
```

Order API:

```text
GET  /api/order-contract
GET  /api/orders/balance
POST /api/orders
GET  /api/orders/{order_id}
GET  /api/orders/{order_id}/events
WS   /ws/orders/{order_id}
```

Order rules:

- `POST /api/orders` requires the `Idempotency-Key` header.
- `GET /api/orders/balance` queries KIS demo overseas orderable cash for the selected symbol/exchange.
- `KIS_ENV=real` is disabled for v1.
- v1 order submit supports KIS overseas demo limit orders only.
- KIS demo credentials are read from AWS Secrets Manager `tead/gops/kis` by default.

Auth rules:

- Set `AUTH_ENABLED=true` to require Google login for `/api/orders`, `/ws/orders/{order_id}`, and `/api/llm/*`.
- Chart and market-data APIs remain public in v1.
- Sessions are stored in Redis and scoped by `AUTH_REDIS_KEY_PREFIX`.
- Google OAuth env values are read directly first; when they are empty, set `GOOGLE_OAUTH_SECRET_NAME` to read them from AWS Secrets Manager.

## Operating Rules

- Chart API serves from Redis and ClickHouse, not directly from S3.
- S3 is durable replay/rematerialization storage.
- ClickHouse `chart_candles` is the serving projection.
- Local runtime must not invent fake market candles.
- Agent-orchestration must not execute orders or call account-control flows.
- Agent provider failures should degrade to no-data evidence instead of crashing the whole analysis path.
- `.env`, access-key CSV files, KIS token caches, `node_modules`, `dist`, and local caches must not be committed.

## Verification

Run the relevant checks before sharing changes:

```sh
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m compileall -q systems
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/market-data/tests
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m pytest systems/order/tests/kis_trader
npm run test:chart --prefix apps/gops-frontend
npm run build --prefix apps/gops-frontend
docker compose config --quiet
kubectl kustomize infra/k8s/base >/tmp/gops-k8s-base.yaml
kubectl kustomize infra/k8s/overlays/aws >/tmp/gops-k8s-aws.yaml
git diff --check
```

Runtime smoke:

```sh
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/api/charts/symbols
curl -fsS 'http://localhost:8000/api/charts/candles?symbol=NVDA&interval=1m&limit=2'
curl -fsS http://localhost:8000/api/order-contract
curl -fsS 'http://localhost:8000/api/orders/balance?symbol=NVDA&exchange=NASD&price=1.00'
```
