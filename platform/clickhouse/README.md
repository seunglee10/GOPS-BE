# ClickHouse Platform Contract

Current local stage:

```text
docker-compose clickhouse
infra/clickhouse/initdb
```

ClickHouse owns the `chart_candles` serving projection.

For the upcoming chart-data rebuild, ClickHouse is the confirmed historical
serving source for ranges older than the Redis latest-120-bar cache. Planned
additional tables are documented in `../../docs/CHART_DATA_REBUILD_PLAN.md`
(`quote_ticks`, `market_events`, `backfill_jobs`, and
`storage_object_audit`). The rebuild must preserve `feed_profile`,
`market_session`, `price_adjustment`, and `canonical_version` metadata.

If `platform/clickhouse/initdb` becomes canonical later, update compose, k8s, and docs in the same change.
