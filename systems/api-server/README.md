# API Server System

Owns the single FastAPI server that brokers chart API, order API, orderable cash lookup, and WebSocket traffic.

## Folders

```text
pods/api-server/   FastAPI application
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
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server \
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

During tick replay the handlers receive the simulator `virtualTime`. Chart-event
and daily-news snapshots are limited to `generated_at <= virtualTime` before
ClickHouse chooses the latest row for each date. `GET /api/market/news/latest`
uses only ClickHouse localized articles with both `published_at` and
`localized_at` at or before the cursor; it never reads or warms the live Redis
cache in SIM. `GET /api/market/news/daily` is also ClickHouse-only in SIM and
does not attach a latest daily price change. Earnings collected after the replay
cursor are excluded. Other latest-only market and news-watchlist routes continue
to return `simulation_data_unavailable`.

## Simulator Quick Orders

`GET /api/simulator/quote?symbol=NVDA` exposes the current replay bid and ask
only while SIM mode is active. The quick-order UI uses that quote instead of the
blocked live order-flow routes, then submits through the existing
`POST /api/orders` contract so fills, balances, and order history stay in the
current simulator `userId + runId` ledger. Symbols outside the replay manifest
remain unavailable rather than receiving synthetic quotes.

## Market Heatmap

`GET /api/market/heatmap?universe=sp500` is the API-owned serving projection for
the frontend TreeMap. It reads the fundamentals store produced by
`systems/fundamentals`: Redis summary key
`gops:fundamentals:summary:v1:{SYMBOL}` first, then ClickHouse
`sec_financial_facts` and `sec_company_tickers` fallback. It combines
`sharesOutstanding` with market prices from the market-data provider and caches
the result in Redis. It does not collect SEC filings directly; that remains a
separate worker/store responsibility.

The public response is intentionally compact for the browser: each item contains
symbol identity, company/sector/industry labels, `marketCap`, `layoutMarketCap`,
current quote fields (`lastPrice`, `previousClose`, `changePercent`), and the
volume fields used by the TreeMap hover panel. The Redis projection may retain
additional fundamentals for internal consumers, but those fields are not sent
through this endpoint. Detailed fundamentals and time series are fetched through
the symbol-specific fundamentals endpoints when a company is selected.

## Imports

The backend imports shared packages by namespace:

```text
alfaka.*      from systems/market-data/shared
kis_trader.*  from systems/order/shared
```

Do not move route behavior or API contracts during structure-only work.
