# API Server System

Owns the single FastAPI server that brokers chart API, order API, orderable cash lookup, and WebSocket traffic.

## Folders

```text
pods/api-server/gops-backend/   FastAPI application
tests/                          API server tests
.env.example                    optional backend-local env example
```

`jobs/` and `shared/` are reserved for future API-server-owned code. Do not add code there unless this system truly owns it.

## Runtime

```text
image:   gops-api-server
docker:  infra/docker/Dockerfile.gops-backend
command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Auth

Google OAuth login is owned by this API server. When `AUTH_ENABLED=true`, the
server protects `/api/orders`, `/ws/orders/{order_id}`, and `/api/llm/*`.
Chart and market-data endpoints remain public.

Sessions are stored in Redis under `AUTH_REDIS_KEY_PREFIX` and the browser only
receives an HttpOnly session id cookie.

The backend reads `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
`AUTH_SESSION_SECRET` from direct env first. If `AUTH_ENABLED=true` and any of
them are empty, `GOOGLE_OAUTH_SECRET_NAME` can point to an AWS Secrets Manager
JSON secret that supplies the missing values.

## Imports

The backend imports shared packages by namespace:

```text
alfaka.*      from systems/market-data/shared
kis_trader.*  from systems/order/shared
```

Do not move route behavior or API contracts during structure-only work.

## Chart Rebuild Notes

The planned chart-data rebuild is documented in
`../../docs/CHART_DATA_REBUILD_PLAN.md`.

API-server responsibilities for that rebuild:

- preserve the existing chart routes listed in root `AGENTS.md`;
- serve chart reads through Redis and ClickHouse, never synchronous S3/Alpaca calls;
- queue missing ranges through `/api/charts/backfill`;
- expose monitor-only JSON endpoints for Redis, S3, ClickHouse, backfill, and duplicate audits;
- keep the frontend from connecting directly to Redis, S3, or ClickHouse.
