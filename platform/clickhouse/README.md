# ClickHouse Platform Contract

Current local stage:

```text
docker-compose clickhouse
infra/clickhouse/initdb
```

ClickHouse owns the `chart_candles` serving projection.

If `platform/clickhouse/initdb` becomes canonical later, update compose, k8s, and docs in the same change.
