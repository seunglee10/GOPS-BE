# Order System

Owns the KIS overseas demo order domain, persistent paper trading, account holdings lookup, outbox publishing, broker adapter, migrations, and reconciliation.

## Folders

```text
pods/order-outbox/       long-running outbox publisher runtime
pods/paper-order-matcher/ realtime quote matcher for persistent paper orders
pods/kis-adapter/        long-running KIS adapter runtime
jobs/migrations/         Postgres migration job
jobs/reconciler/         limited/manual reconciliation job
shared/kis_trader/       order import namespace
tests/kis_trader/        order tests
```

SQL migration files currently live in `shared/kis_trader/migrations` because runtime code imports them through `kis_trader.persistence.migrations`.

## Runtime Entrypoints

```text
pods/order-outbox/main.py    wraps kis_trader.cli outbox-publish --limit 100 in a 2 second loop
pods/paper-order-matcher/main.py consumes market.layer.quotes.v1 and fills paper limit orders
pods/kis-adapter/main.py     wraps kis_trader.cli broker-adapter --timeout-seconds 1.0
jobs/migrations/main.py      wraps kis_trader.cli migrate
jobs/reconciler/main.py      wraps kis_trader.cli reconcile --rows-json []
```

`kis-adapter` uses the real KIS demo HTTP client by default. Set
`KIS_BROKER_ADAPTER_ARGS=--fake-kis success` only when running an explicit local
fake smoke.
v1 accepts overseas demo limit orders only, exposes KIS overseas orderable cash
for the order ticket, and reads KIS credentials from AWS Secrets Manager
`tead/gops/kis` by default.

## Images

```text
gops-order-worker   order-outbox, paper-order-matcher, migrations, reconciler
gops-kis-adapter    kis-adapter
```

## Platform Dependencies

```text
Postgres
Kafka
Redis realtime subscription control plane
Secrets Manager / KIS demo credentials
KIS demo API
```

Keep `kis_trader.*` imports stable. Docker, compose, k8s, tests, and local scripts should place `systems/order/shared` on `PYTHONPATH`.

`KIS_ENV=real` remains disabled for v1.

## Persistent Paper Trading

`kis_trader.paper` is independent from both KIS and the short-lived GOPS demo
simulator. `POST /api/paper/orders` requires `Idempotency-Key`, reserves paper
cash or whole-share holdings in Postgres, and never writes the KIS order outbox.
The matcher fills a buy at the current ask when `ask <= limit`, or a sell at the
current bid when `bid >= limit`. Orders remain pending until filled, cancelled,
or removed by an explicit account reset.

Migration `0006_paper_trading.sql` creates account generations, positions,
orders, events, and an append-only cash ledger. Deploy it before starting
`paper-order-matcher`. The matcher also keeps pending-order symbols in the
highest-priority Alpaca realtime subscription cohort; the configured default is
at most 100 distinct pending symbols.

The frontend-facing account holdings view uses `GET /api/account/holdings`, which calls KIS demo balance APIs through `kis_trader.kis.client.DemoKisHttpClient` and reuses the existing `dev/kis` Secrets Manager contract.

The overseas holdings balance response does not treat `frcr_buy_amt_smtl1`
(foreign-currency buy amount sum) as cash. When KIS does not provide a genuine
foreign-currency cash field, `account.cashForeign` remains `null`; stock and
total foreign values are derived from the position valuation sum instead of
fabricating a cash allocation.
