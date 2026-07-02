# Local Docker Onboarding

This is the first document to follow after cloning the repo.
It focuses on reproducing the local runtime without creating fake chart data.

## 1. Prerequisites

- Docker Desktop is installed and running.
- Python `3.12.x` is available.
- AWS credentials with access to Secrets Manager and S3 are available when using real Alpaca/S3 paths.
- Ports `5173`, `8000`, `8123`, `9092`, `6379`, and `5433` are free.

## 2. Create `.env`

```sh
cp .env.example .env
```

Keep these values aligned for the chart-data rewrite:

```text
AWS_REGION=ap-northeast-2
AWS_DEFAULT_REGION=ap-northeast-2
AWS_PROFILE=default

ALPACA_SECRET_NAME=dev/alpaca
ALPACA_FEED_PROFILES=sip,boats
ALPACA_ENFORCE_FEED_SESSION_WINDOW=true

REDIS_KEY_PREFIX=gops:market:on-demand:v1

S3_BUCKET=gops-market-data-<aws-account-id>-ap-northeast-2-an
S3_ENDPOINT_URL=
DOCKER_S3_ENDPOINT_URL=
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_LIVE_PREFIX=market-data/rebuild-20260702-lazy-v1/live
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final
```

`S3_RAW_PREFIX` is backup-only. The chart API, backfill coverage, and ClickHouse
materialization must not depend on raw S3 objects.

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

## 5. Live And Backfill

Do not preload chart data. Start realtime ingestion only when testing realtime
chart behavior:

```sh
docker compose --profile alpaca up -d --build
```

SIP and BOATS must be exclusive writers:

```text
04:00 - 20:00 ET = SIP only
20:00 - 04:00 ET = BOATS only
```

Backfill should be queued only for missing requested chart ranges.

## 6. Smoke Checks

```sh
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/health/config
curl -fsS http://localhost:8000/api/charts/symbols
curl -fsS 'http://localhost:8000/api/charts/candles?symbol=AAPL&interval=1m&limit=120'
curl -fsS http://localhost:8000/api/order-contract
```

`/health/config` must show only safe `SET`/`EMPTY` values for credentials.
It must never print secret values.

## 7. Common Failures

| Symptom | Check |
| --- | --- |
| Docker services do not start | Confirm Docker Desktop is running and required ports are free. |
| Backend cannot read Alpaca credentials | Check AWS keys and the `dev/alpaca` secret JSON shape. |
| S3 write/read fails | Check bucket name, region, IAM permission, and real AWS endpoint values are empty. |
| Chart has no candles | This is valid after reset. Request backfill only for the visible range. |
| Live stream shows idle | Market may be closed, no active chart subscription may exist, or the non-active feed may be idle by design. |

If a local access-key CSV exists in the repo root, remove it after copying
values into `.env`.
