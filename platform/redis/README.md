# Redis Platform Contract

Current local stage:

```text
docker-compose redis
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=
```

AWS/EKS can later point `REDIS_URL` at ElastiCache, Valkey, or another Redis-compatible endpoint.

Local compose and the in-cluster Redis StatefulSet run Redis as chart/live/backfill
state, not as the durable historical source. S3 and ClickHouse are the durable
market-data stores. Keep Redis from blocking writes because of background RDB
snapshot failures:

```text
redis-server --appendonly yes --save "" --stop-writes-on-bgsave-error no
```

Do not use `FLUSHALL` for chart resets. Delete only the documented
chart/market-data key patterns so auth, order, and agent state can be preserved.

The API server also stores Google login sessions in Redis when `AUTH_ENABLED=true`.
Session keys use `AUTH_REDIS_KEY_PREFIX` (`gops:auth` by default) and TTLs, so no
separate Redis deployment is required for auth.

## Planned Chart Rebuild Keys

The upcoming chart-data rebuild is specified in
`../../docs/CHART_DATA_REBUILD_PLAN.md`. Its Redis contract is intentionally
bounded:

- `gops:market:on-demand:v1:cache:candles:{symbol}:{interval}` keeps only the newest 120 candles;
- `live:candle`, `latest:closed:candle`, `state:candle-window`, and `pending:replace` hold realtime/replacement state;
- `backfill:*` keys hold Redis Streams queue, locks, status, and dead-letter state;
- `feed:*` keys coordinate exclusive SIP/BOATS writer ownership.

Do not treat Redis as a historical chart database.
