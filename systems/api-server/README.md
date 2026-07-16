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

For a backend-only local process, start from the repository root and pass the
ignored local file explicitly so every chart module receives the same process
environment:

```bash
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --env-file systems/api-server/.env
```

`systems/api-server/.env.example` is the committed contract. Docker Compose
uses the repository-root `.env.example` contract instead; real `.env` files
remain untracked.

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

## Chart Events

`GET /api/charts/events` returns stored earnings and daily-news markers for the
loaded candle range. The handler reads ClickHouse only: S&P 500 earnings come
from `yahoo_earnings_estimates`, and news comes from
`news_company_daily_summaries`. Non-S&P 500 symbols return news with an empty
earnings state instead of an error. Yahoo and Alpaca are never called by this
request.

## Market Heatmap

`GET /api/market/heatmap?universe=sp500` is the API-owned serving projection for
the frontend TreeMap. It reads the fundamentals store produced by
`systems/fundamentals`: Redis summary key
`gops:fundamentals:summary:v1:{SYMBOL}` first, then ClickHouse
`sec_financial_facts` and `sec_company_tickers` fallback. It combines
`sharesOutstanding` with market prices from the market-data provider and caches
the result in Redis. It does not collect SEC filings directly; that remains a
separate worker/store responsibility.

The expected minimum data is a `shares_outstanding` metric plus symbol identity.
`companyName`, `sector`, `industry`, `cik`, `periodEndDate`, and `filedAt` are
used when available; seed classification remains the fallback.

## Imports

The backend imports shared packages by namespace:

```text
alfaka.*      from systems/market-data/shared
kis_trader.*  from systems/order/shared
```

Do not move route behavior or API contracts during structure-only work.
