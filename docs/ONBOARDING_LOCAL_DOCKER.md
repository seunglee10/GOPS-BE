# Local Docker Onboarding

This is the first document to follow after cloning the repo.
It is intentionally short and focuses on reproducing the local Docker runtime.

## 1. Prerequisites

- Docker Desktop is installed and running.
- Python `3.12.x` is available.
- AWS credentials with access to Secrets Manager and S3 are available.
- Ports `5173`, `8000`, `8123`, `9092`, `6379`, and `5433` are free.

## 2. Create `.env`

```sh
cp .env.example .env
```

For the default local Docker flow, GOPS uses real AWS S3 and AWS Secrets Manager.
Keep these values aligned:

```text
AWS_REGION=ap-northeast-2
AWS_DEFAULT_REGION=ap-northeast-2
AWS_ACCESS_KEY_ID=<your restricted local key>
AWS_SECRET_ACCESS_KEY=<your restricted local secret>
AWS_SESSION_TOKEN=

ALPACA_SECRET_NAME=dev/alpaca
S3_BUCKET=gops-market-data-<aws-account-id>-ap-northeast-2-an
S3_ARCHIVE_ROOT_PREFIX=market-data/dev/helixho
S3_ENDPOINT_URL=
DOCKER_S3_ENDPOINT_URL=
```

The default S3 archive namespace is `market-data/dev/helixho/...`; keep it isolated unless intentionally sharing objects with another teammate.

`dev/alpaca` must be a JSON secret:

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
| `alpaca` | SIP live Alpaca ingestion and symbol registry sync. Use only when live market ingestion is needed. |
| `alpaca-iex`, `alpaca-boats` | Optional extra Alpaca feed ingestors. Use only after confirming the Alpaca account can open additional WebSocket feeds. |
| `graphdb` | Optional GraphDB runtime for ontology evidence. |
| `local-s3` | MinIO experiments only. Not the default path. |
| `reconciliation` | Manual order reconciliation job. |

Start live Alpaca ingestion only when needed:

```sh
docker compose --profile alpaca up -d --build \
  alpaca-ingestor alpaca-news-ingestor symbol-registry-sync
```

Start optional extra feeds only after checking the Alpaca account connection limit:

```sh
docker compose --profile alpaca-iex up -d --build alpaca-ingestor-iex
docker compose --profile alpaca-boats up -d --build alpaca-ingestor-boats
```

## 6. Smoke Checks

```sh
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/health/config
curl -fsS http://localhost:8000/api/charts/symbols
curl -fsS 'http://localhost:8000/api/charts/candles?symbol=AAPL&interval=1m&limit=2'
curl -fsS http://localhost:8000/api/order-contract
```

`/health/config` must show only safe `SET`/`EMPTY` values for credentials.
It must never print secret values.

## 7. Common Failures

| Symptom | Check |
| --- | --- |
| Docker services do not start | Confirm Docker Desktop is running and required ports are free. |
| Backend cannot read Alpaca credentials | Check AWS keys and the `dev/alpaca` secret JSON shape. |
| S3 archive write fails | Check bucket name, region, IAM permission, endpoint values, and `S3_ARCHIVE_ROOT_PREFIX`. Chart rendering should still use Redis/ClickHouse. |
| Chart has no candles | Open the requested chart range and let range backfill queue the missing ClickHouse buckets. |
| Live stream shows idle | This can mean WebSocket is connected but no current market data is arriving yet. Stored candles should still render. |
| Order submit path fails locally | Keep `KIS_ENV=demo` and the default fake KIS adapter args unless intentionally testing KIS demo. |

If a local access-key CSV exists in the repo root, remove it after copying values into `.env`.
