# Order System

Owns the KIS demo order domain, outbox publishing, broker adapter, migrations, and reconciliation.

## Folders

```text
pods/order-outbox/       long-running outbox publisher runtime
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
pods/kis-adapter/main.py     wraps kis_trader.cli broker-adapter --timeout-seconds 1.0
jobs/migrations/main.py      wraps kis_trader.cli migrate
jobs/reconciler/main.py      wraps kis_trader.cli reconcile --rows-json []
```

`kis-adapter` uses the real KIS demo HTTP client by default. Set
`KIS_BROKER_ADAPTER_ARGS=--fake-kis success` only when running an explicit local
fake smoke.

## Images

```text
gops-order-worker   order-outbox, migrations, reconciler
gops-kis-adapter    kis-adapter
```

## Platform Dependencies

```text
Postgres
Kafka
Secrets Manager / KIS demo credentials
KIS demo API
```

Keep `kis_trader.*` imports stable. Docker, compose, k8s, tests, and local scripts should place `systems/order/shared` on `PYTHONPATH`.

`KIS_ENV=real` remains disabled for v1.
