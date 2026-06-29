# API Server System

Owns the single FastAPI server that brokers chart API, order API, and WebSocket traffic.

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

## Imports

The backend imports shared packages by namespace:

```text
alfaka.*      from systems/market-data/shared
kis_trader.*  from systems/order/shared
```

Do not move route behavior or API contracts during structure-only work.
