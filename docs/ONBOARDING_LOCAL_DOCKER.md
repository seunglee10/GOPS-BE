# Local Docker Onboarding

This is the first document to follow after cloning the repo.
It is intentionally short and focuses on reproducing the local Docker runtime.

## 1. Prerequisites

- Docker Desktop is installed and running.
- Python `3.12.x` is available.
- AWS credentials with S3 access are available when using real AWS S3.
- Alpaca API keys are available either as local `APCA_*` env values or in AWS Secrets Manager.
- Ports `5173`, `8000`, `8123`, `9092`, `6379`, and `5433` are free.

## 2. Create `.env`

```sh
cp .env.example .env
```

For local Alpaca smoke while Secrets Manager is disconnected, put Alpaca keys
only in your uncommitted `.env` and use `ALPACA_CREDENTIAL_SOURCE=local-env`.
Compose mounts the host `~/.aws` directory read-only into the API, backfill,
and optional Alpaca live-ingestion services for S3/AWS-contract runs. Keep these
values aligned:

```text
AWS_REGION=ap-northeast-2
AWS_DEFAULT_REGION=ap-northeast-2
AWS_PROFILE=default
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=

APCA_API_KEY_ID=
APCA_API_SECRET_KEY=
ALPACA_CREDENTIAL_SOURCE=local-env
ALPACA_SECRET_NAME=
S3_BUCKET=gops-market-data-<aws-account-id>-ap-northeast-2-an
S3_ENDPOINT_URL=
DOCKER_S3_ENDPOINT_URL=
```

For AWS/EKS or an AWS-contract local run, keep `APCA_*` empty, set
`ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager`, and set
`ALPACA_SECRET_NAME=dev/alpaca`. `dev/alpaca` must be a JSON secret:

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

Do not commit `.env`, access-key CSV files, token caches, or copied secrets.

## 3. Python Environment

Use one repo-local virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python --version
```

Expected version: `3.12.x`.

## 4. Start Docker

```sh
docker compose --env-file .env up -d --build
```

Open:

```text
Frontend: http://localhost:5173
Backend:  http://localhost:8000/health
Config:   http://localhost:8000/health/config
```

## 5. Profiles

| Profile | Use |
| --- | --- |
| default | Backend, frontend, Kafka, Redis, Postgres, ClickHouse, storage workers, backfill worker. |
| `alpaca` | Live Alpaca ingestion and symbol registry sync. Use only when live market ingestion is needed. |
| `repair` | Coverage audit and missing chart backfill queue. Dry-run by default. |
| `local-s3` | MinIO experiments only. Not the default path. |
| `reconciliation` | Manual order reconciliation job. |

Start live Alpaca ingestion only when real-time charts are needed. The default
contract uses SIP for `04:00-20:00 ET`, BOATS for `20:00-04:00 ET`,
and a separate 24/7 crypto ingestor for `BTCUSD`:

```sh
docker compose --profile alpaca up -d --build alpaca-ingestor alpaca-ingestor-boats alpaca-ingestor-crypto
```

Audit chart coverage:

```sh
docker compose --profile repair run --rm coverage-repair
```

Queue missing backfills intentionally:

```sh
COVERAGE_REPAIR_DRY_RUN=false docker compose --profile repair run --rm coverage-repair
```

## 6. Smoke Checks

```sh
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/health/config
curl -fsS http://localhost:8000/api/charts/symbols
curl -fsS 'http://localhost:8000/api/charts/candles?symbol=NVDA&interval=1m&limit=2'
curl -fsS http://localhost:8000/api/order-contract
curl -fsS 'http://localhost:8000/api/orders/balance?symbol=NVDA&exchange=NASD&price=1.00'
docker compose --profile repair run --rm coverage-repair
```

`/health/config` must show only safe `SET`/`EMPTY` values for credentials.
It must never print secret values.

## 7. Common Failures

| Symptom | Check |
| --- | --- |
| Docker services do not start | Confirm Docker Desktop is running and required ports are free. |
| Backend cannot read Alpaca credentials | Check AWS keys and the `dev/alpaca` secret JSON shape. |
| S3 write/read fails | Check bucket name, region, IAM permission, and that S3 endpoint values are empty for real AWS. |
| Chart has no candles | Run the `repair` profile dry-run, then queue missing backfills if needed. |
| Live stream shows idle | This can mean WebSocket is connected but no current market data is arriving yet. Stored candles should still render. |
| Order submit path fails locally | Keep `KIS_ENV=demo`, `KIS_CREDENTIAL_SOURCE=aws-secrets-manager`, and check the `tead/gops/kis` secret JSON shape. Use `KIS_BROKER_ADAPTER_ARGS=--fake-kis success` only for explicit fake smoke runs. |

If a local access-key CSV exists in the repo root, remove it after copying values into `.env`.
