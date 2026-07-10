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
gops:market:on-demand:v1:order-flow:{symbol}:minutes
gops:market:on-demand:v1:order-flow:{symbol}:live-minute
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
chart:indicators:{version}:{symbol}:{interval}:{requestHash}
chart:volume-profile:{version}:{symbol}:{requestHash}
chart:derived:lock:{requestHash}
```

`order-flow:{symbol}:minutes` stores closed minute blobs in a ZSET for at most
`ORDER_FLOW_LIVE_TTL_SECONDS` (default 86400). `live-minute` stores one current
minute blob for `ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS` (default 300); it replaces
the same minute read from the ZSET. The API combines those keys with ClickHouse
daily rows, and `ORDER_FLOW_BINS_UPDATE` uses the existing `market.events`
pub/sub fanout.

Indicator and candle-volume-profile results are API-owned request-time caches.
Their result keys expire after 300s/30s by default. The singleflight lock uses
`SET NX EX` and defaults to 30s. There is no durable derived-result Redis state.

| Family | Writer | Reader | Bound | Version |
| --- | --- | --- | --- | --- |
| recent/closed candles | market processor | canonical candle provider | 120 rows per symbol/interval + 7d watermark TTL | on-demand v1 |
| live candle/trade/quote/event | market/quote processors | API/WS/realtime cohorts | 180s or explicit live TTL | on-demand v1 |
| order-flow minute blobs | market processor | order-flow API | 86400s closed / 300s current | minute-blob v2 |
| indicator/profile cache | API derived service | chart routes | 300s / 30s | calculation version in key |
| derived lock | API derived service | API replicas | 30s | request hash |
| subscription/feed state | API/controller | ingestors/processors | TTL or bounded symbol set | on-demand v1 |

Legacy keys such as `price:*`, `candle:*`, and `candles:*` are reset targets
only; do not add new chart state that depends on them. `market.events*` remains
the existing pub/sub fanout path for live updates, not durable Redis state.

The feed controller may also maintain compatibility helper keys
`feed:active:profile` and `feed:active:epoch`, but `feed:active` is the
canonical state read by chart feed guards.

The API server also stores Google login sessions in Redis when `AUTH_ENABLED=true`.
Session keys use `AUTH_REDIS_KEY_PREFIX` (`gops:auth` by default) and TTLs, so no
separate Redis deployment is required for auth.

## Fundamentals Keys

SEC fundamentals summaries are cache projections written by
`systems/fundamentals/jobs/sec-companyfacts-backfill` and future reconcile jobs,
then read by the Financial Agent. They are not the durable source of truth;
ClickHouse `market_data.sec_*` tables are.

```text
gops:fundamentals:summary:v1:{SYMBOL}
gops:fundamentals:peer:v1:{SYMBOL}:latest
gops:fundamentals:peer:v1:{SYMBOL}:{FRAME_PERIOD}
gops:agent:financial-final-answer:v1:{SYMBOL}:{digest}
```

The `:latest` peer key is an alias written by the backfill/reconcile job.
Runtime reads it first and uses the payload `frame_period` to show the
comparison basis. Runtime does not query ClickHouse to stale-check Redis hits;
stale checks belong to the backfill/nightly reconcile job.

The `financial-final-answer` key caches the LLM-written Korean report generated
from a specific formatted facts/signals payload. Its digest changes when the
snapshot facts, route, or limitations change.
