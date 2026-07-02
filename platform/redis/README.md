# Redis Platform Contract

Current local stage:

```text
docker-compose redis
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=gops:market:on-demand:v1
```

AWS/EKS can later point `REDIS_URL` at ElastiCache, Valkey, or another Redis-compatible endpoint.

Local compose and the in-cluster Redis StatefulSet run Redis as chart/live,
replacement, feed-control, and backfill state, not as the durable historical
source. Redis keeps only the newest 120 confirmed candles per
`symbol + timeframe`; older confirmed candles are read from ClickHouse. S3
final/manifest is durable rebuild evidence. Keep Redis from blocking writes
because of background RDB snapshot failures:

```text
redis-server --appendonly yes --save "" --stop-writes-on-bgsave-error no
```

Do not use `FLUSHALL` for chart resets. Delete only the documented
chart/market-data key patterns so auth, order, and agent state can be preserved.

Canonical chart-data key families:

```text
gops:market:on-demand:v1:cache:candles:{symbol}:{interval}
gops:market:on-demand:v1:live:candle:{symbol}:{interval}
gops:market:on-demand:v1:latest:closed:candle:{symbol}:{interval}
gops:market:on-demand:v1:state:candle-window:{symbol}:{interval}:{bucket}
gops:market:on-demand:v1:pending:replace:{symbol}:{interval}:{timestamp}
gops:market:on-demand:v1:live:trade:{symbol}
gops:market:on-demand:v1:live:quote:{symbol}
gops:market:on-demand:v1:live:event:{symbol}
gops:market:on-demand:v1:subscription:symbols
gops:market:on-demand:v1:subscription:symbol:{symbol}
gops:market:on-demand:v1:subscription:version
gops:market:on-demand:v1:subscription:events
gops:market:on-demand:v1:backfill:*
gops:market:on-demand:v1:feed:active
gops:market:on-demand:v1:feed:lease:sip
gops:market:on-demand:v1:feed:lease:boats
gops:market:on-demand:v1:feed:switch:state
gops:market:on-demand:v1:feed:quarantine:{date}
```

Legacy keys such as `price:*`, `candle:*`, `candles:*`, and `market.events*`
are reset targets only; do not add new chart code that depends on them.

The feed controller may also maintain compatibility helper keys
`feed:active:profile` and `feed:active:epoch`, but `feed:active` is the
canonical state read by chart feed guards.

The API server also stores Google login sessions in Redis when `AUTH_ENABLED=true`.
Session keys use `AUTH_REDIS_KEY_PREFIX` (`gops:auth` by default) and TTLs, so no
separate Redis deployment is required for auth.
