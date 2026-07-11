# GOPS Environment And Platform Contracts

This file documents platform dependencies and env contracts.
Do not put real secrets here.

For chart-data work, `docs/CHART_DATA_ARCHITECTURE.md` is the source of truth;
operator procedures live in `docs/CHART_DATA_OPERATIONS.md`.
The current runtime uses a hybrid collection model: S&P500 baseline
bars/updatedBars/dailyBars/statuses stay subscribed for list prices and fast
chart entry, while realtime trades/quotes are limited to explicit cohorts such
as watchlist, portfolio, rankings, active chart sessions, and manual admin
subscriptions.

## Platform Folders

```text
platform/kafka/
platform/redis/
platform/postgres/
platform/clickhouse/
platform/s3/
platform/secrets/
```

Each folder explains local behavior, possible next stages, env vars, and related compose/k8s/AWS assets.

## Alpaca Collection

Current chart rebuild contract:

```text
ALFAKA_REQUEST_CONFIG=systems/market-data/config/market-data-request.json
ALPACA_UNIVERSE=sp500
ALPACA_UNIVERSE_REGISTRY_PATH=systems/market-data/config/sp500-universe.json
ALPACA_COLLECTION_SYMBOL_SOURCE=universe
ALPACA_CHANNELS=bars,updatedBars,dailyBars,statuses
ALPACA_ACTIVE_CHANNELS=trades,quotes
ALPACA_MAX_TRADE_SYMBOLS=100
ALPACA_KAFKA_PUBLISH_WORKERS=4
ALPACA_KAFKA_PUBLISH_QUEUE_MAXSIZE=20000
ALPACA_KAFKA_QUEUE_PUT_TIMEOUT_SECONDS=0.25
ALPACA_KAFKA_PUBLISH_STOP_TIMEOUT_SECONDS=5
KAFKA_PRODUCER_LINGER_MS=20
KAFKA_PRODUCER_BATCH_SIZE=65536
KAFKA_PRODUCER_MAX_BLOCK_MS=3000
KAFKA_PRODUCER_ACKS=1
ALPACA_FEED_PROFILE=sip
ALPACA_FEED_PROFILES=sip,boats
ALPACA_ENFORCE_FEED_SESSION_WINDOW=true
ALPACA_SESSION_IDLE_POLL_SECONDS=60
ALPACA_CREDENTIAL_SOURCE=local-env
ALPACA_SECRET_NAME=
HOT_TIER_SIZE=10
HOT_TIER_FALLBACK_SCAN_LIMIT=20
```

Baseline collection is SIP-only and bars/statuses-only, with active ticks on
the same SIP WebSocket: the SIP ingestor subscribes the S&P500 universe for
`bars`, `updatedBars`, `dailyBars`, and `statuses` so every S&P500 chart has a
fast recent 1m entry path. Runtime `trades` and `quotes` still follow the exact
same explicit symbol set as realtime cohorts: watchlist, portfolio, rankings,
active chart sessions, and manual admin subscriptions. Quotes are never a
separate all-symbol feed. AWS/EKS keeps these SIP responsibilities inside
`alfaka-alpaca-ingestor-sip` so Alpaca SIP connection limits are not consumed by
two simultaneous WebSocket clients.
`ALPACA_MAX_TRADE_SYMBOLS` caps the realtime tick cohort and the ingestor
prioritizes active chart, manual, portfolio, watchlist, then ranking symbols
when the cap is reached.

Alpaca raw Kafka publishing is decoupled from WebSocket receive by an async
queue. `ALPACA_KAFKA_PUBLISH_WORKERS` controls how many background publisher
tasks drain that queue, `ALPACA_KAFKA_PUBLISH_QUEUE_MAXSIZE` limits in-process
buffering, `ALPACA_KAFKA_QUEUE_PUT_TIMEOUT_SECONDS` bounds how long the receive
loop waits when Kafka is unhealthy, and
`ALPACA_KAFKA_PUBLISH_STOP_TIMEOUT_SECONDS` bounds shutdown drain time. Kafka
producer batching is tuned with `KAFKA_PRODUCER_LINGER_MS`,
`KAFKA_PRODUCER_BATCH_SIZE`, and related `KAFKA_PRODUCER_*` env vars.

BOATS/overnight keeps `ALPACA_COLLECTION_SYMBOL_SOURCE=on-demand` at the
deployment level. Overnight liquidity is sparse and the BOATS stream should not
fan out a 500-symbol baseline until feed support and traffic are measured.

`ALPACA_FEED_PROFILE` selects one active ingestor runtime feed (`sip` or
`boats`). The live contract is session-routed: SIP is primary for `04:00-20:00
ET` (`pre`, `regular`, `after`), and BOATS is primary for `20:00-04:00 ET`
(`overnight`). Sunday `20:00 ET` opens the Monday overnight slice; Friday
`20:00 ET` closes the equity 24/5 window. Local compose and k8s run one ingestor
per active profile, and `/health/config` reports the expected profile set from
`ALPACA_FEED_PROFILES`. Market-data envelopes, Redis live state, ClickHouse
candle rows, API candles, and chart snapshots preserve `feedProfile` and
`marketSession` so daytime and BOATS/overnight data remain diagnosable.

`ALPACA_CREDENTIAL_SOURCE` accepts `auto`, `aws-secrets-manager`, or `local-env`. Use `local-env` with `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` for explicit local Alpaca smoke runs while Secrets Manager is disconnected. AWS/EKS overlays set `aws-secrets-manager` and read the same canonical key names from the `dev/alpaca` JSON secret. Alpaca ingestors read credentials immediately before each WebSocket connect attempt, so a Secrets Manager rotation is picked up on the next reconnect or session open.

`ALPACA_WS_PING_INTERVAL_SECONDS` and `ALPACA_WS_PING_TIMEOUT_SECONDS` control
Alpaca WebSocket keepalive behavior. AWS/EKS defaults to `30` and `60` seconds
to avoid unnecessary reconnect loops when the SIP stream is busy. Set either to
`off` only for a controlled incident mitigation.

## Saturday Demo Simulator

The five-minute local demonstration uses the safe values in
`config/demo-simulator.env`. `GOPS_SIMULATOR_URL` connects the GOPS backend to
the dummy account and order ledger, while `ALPACA_STREAM_BASE_URL` points the
market ingestor at the Alpaca-compatible simulator WebSocket. The demo profile
subscribes only `NVDA,AMD,AVGO,MU,TSM,XOM,CVX,COP` and disables the live-market
session window so the recorded ticks can play at any rehearsal time.

로컬 통합 스택은 sibling simulator repository에서 시작하고 종료한다:

```text
../gops_simul/scripts/run_demo_stack.sh
../gops_simul/scripts/stop_demo_stack.sh
```

The values in this profile are local test credentials. Do not replace them
with broker credentials: SIM orders must stay inside the simulator ledger and
must never create a KIS order outbox entry.

dev EKS에서는 시뮬레이터를 내부 `ClusterIP` Service로만 배포한다. 기본
`replicas`는 0이라서 평소에는 Pod CPU/메모리를 사용하지 않는다. 시연 직전에
다음 명령으로 시뮬레이터 Pod 하나를 켜고, GOPS backend와 주식 SIP 수집기만
시뮬레이터로 전환한다. BOATS와 crypto 수집기는 건드리지 않는다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/start-dev-simulator.sh
```

시연 종료 직후 실제 Alpaca SIP 경로를 복구하고 Pod를 다시 0개로 내린다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/stop-dev-simulator.sh
```

실행 중 resource request는 CPU `50m`(코어의 5%)와 메모리 `64Mi`, limit은
CPU `250m`와 메모리 `128Mi`다. 이미지나 manifest를 다시 배포해도 기본값인
0개로 돌아가므로 다음 시연 전에는 start 명령을 다시 실행해야 한다.

## Kafka

Current local stage:

```text
docker-compose kafka
docker-compose kafka-init
platform/kafka/topics.txt
```

Topic contract:

```text
market.input.realtime.trades.v1
market.input.realtime.quotes.v1
market.input.realtime.bars.1m.v1
market.input.realtime.updated-bars.1m.v1
market.input.realtime.daily-bars.v1
market.input.realtime.events.v1
market.realtime.ticks.to.1m.v1
market.realtime.ticks.to.5m.v1
market.realtime.ticks.to.10m.v1
market.realtime.ticks.to.1d.v1
market.realtime.ticks.to.1w.v1
market.realtime.ticks.to.1mo.v1
market.layer.candles.closed.v1
market.layer.candles.1m.closed.v1
market.layer.candles.5m.closed.v1
market.layer.candles.10m.closed.v1
market.layer.candles.1h.closed.v1
market.layer.candles.4h.closed.v1
market.layer.candles.1d.closed.v1
market.layer.candles.1w.closed.v1
market.layer.candles.1mo.closed.v1
market.layer.trades.v1
market.layer.quotes.v1
market.layer.events.v1
market.news.alpaca.v1
orders.commands.v1
broker.submit-results.v1
broker.order-events.v1
orders.dlq.v1
agents.market-events.v1
agents.analysis-requests.v1
agents.analysis-results.v1
agents.notification-decisions.v1
agents.dlq.v1
```

`market.layer.candles.closed.v1` is retained as a legacy compatibility topic.
New market-processor config publishes closed candles to the interval-specific
`market.layer.candles.<interval>.closed.v1` topics.
`market.input.realtime.trades.v1` and `market.input.realtime.quotes.v1` are hot
raw topics and should be created with more partitions than the rest of the
topic set. Local and AWS helpers default them to 12 partitions; standard topics
remain 3 locally and use the normal AWS `PARTITIONS` value.

Do not force MSK as the next step. The staged path is:

```text
local compose -> single Kafka pod candidate -> MSK candidate
```

## Python / Kubernetes Stream Processing

Current repository stage:

```text
systems/market-data/pods/market-processor/local_main.py
infra/k8s/base/app/deployment-market-processor.yaml
infra/k8s/base/app/deployment-market-quote-processor.yaml
```

Runtime path:

```text
local Python processors -> explicit Kubernetes processor pods
```

Common stream processor env:

```text
KAFKA_BOOTSTRAP_SERVERS
KAFKA_INPUT_TOPIC_PREFIX
KAFKA_PROCESSOR_GROUP_ID
KAFKA_PROCESSOR_RAW_TOPICS
KAFKA_PROCESSOR_ENABLE_AUTO_COMMIT
CANDLE_WATERMARK_GRACE_SECONDS
CANDLE_FLUSH_INTERVAL_SECONDS
LIVE_CANDLE_PUBLISH_MIN_INTERVAL_SECONDS
PROCESSOR_ACTIVE_FEED_CACHE_SECONDS
REDIS_URL
PROCESSOR_RECOVERY_SYMBOLS
PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED
COMPONENT_HEALTH_TTL_SECONDS
KAFKA_TICK_FANOUT_INTERVALS
KAFKA_PUBLISH_TICK_FANOUT
LIVE_CANDLE_TTL_SECONDS
LIVE_TRADE_TTL_SECONDS
LIVE_CANDLE_STALE_SECONDS
SYMBOL_LIVE_PRICE_STALE_SECONDS
SYMBOL_REDIS_INTRADAY_STALE_SECONDS
ACTIVE_CHART_TTL_SECONDS
REALTIME_REDIS_POLL_SECONDS
ORDER_FLOW_PINNED_SYMBOLS
ORDER_FLOW_PRICE_BIN_SIZE
ORDER_FLOW_QUOTE_REFRESH_MS
ORDER_FLOW_QUOTE_MAX_AGE_MS
ORDER_FLOW_QUOTE_FUTURE_TOLERANCE_MS
ORDER_FLOW_PUBLISH_THROTTLE_MS
ORDER_FLOW_REDIS_FLUSH_MS
ORDER_FLOW_LIVE_TTL_SECONDS
ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS
ORDER_FLOW_QUOTE_CACHE_ONLY
QUOTE_REDIS_WRITE_MIN_INTERVAL_MS
QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS
TRADE_REDIS_WRITE_MIN_INTERVAL_MS
HEALTH_WRITE_MIN_INTERVAL_MS
```

`PROCESSOR_RECOVERY_SYMBOLS` is optional. Keep it empty unless an incident repair
needs explicit symbols. Baseline S&P500 collection is performed by the ingestor;
processor recovery should still avoid broad ClickHouse recovery unless an
operator explicitly enables it.

`PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED=false` by default. Keep Redis-first recovery on by default; enable ClickHouse recovery only when the processor should rebuild missing startup state from deterministic canonical `1m`/`1D` rows and ClickHouse is known healthy.

`KAFKA_PROCESSOR_RAW_TOPICS` optionally narrows one processor runtime to a
comma-separated raw topic list. AWS/EKS uses this to run
`alfaka-market-processor` on trades/bars/events and
`alfaka-market-quote-processor` on quotes in separate consumer groups. When the
override is present, the processor does not subscribe to legacy tick fanout
topics even if `KAFKA_TICK_FANOUT_INTERVALS` is set.

`COMPONENT_HEALTH_TTL_SECONDS` controls how long Redis keeps lightweight component freshness heartbeats such as `pipeline:health:market-processor`.
The processor also writes scoped health keys such as
`pipeline:health:market-processor:symbol:NVDA` and
`pipeline:health:market-processor:feed:sip` so one noisy feed or symbol does not
hide another symbol's live-path diagnosis.

`KAFKA_TICK_FANOUT_INTERVALS` should normally stay empty in AWS/EKS. The
processor now handles raw trade ticks directly on the 1m/live path and derives
5m, 10m, 1h, 4h, 1D, 1W, and 1M provisional candles from local 1m state. Tick
fanout topics are retained for legacy/debug use only; set
`KAFKA_PUBLISH_TICK_FANOUT=true` deliberately if another consumer group needs
that stream.

`LIVE_CANDLE_PUBLISH_MIN_INTERVAL_SECONDS` throttles Redis/WebSocket/Kafka live
candle publish per `symbol + interval` while still updating in-memory candle
state on every accepted trade. `PROCESSOR_ACTIVE_FEED_CACHE_SECONDS` avoids a
Redis active-feed lookup for every single tick.

Order-flow profile env controls the pinned bid/ask volume profile path.
`ORDER_FLOW_PINNED_SYMBOLS` defaults to `NVDA,AMZN,MU,AAPL,GOOGL` and is the
only v1 live/EOD coverage set. `ORDER_FLOW_PRICE_BIN_SIZE` defaults to `0.01`.
`ORDER_FLOW_QUOTE_REFRESH_MS` gates quote cache refresh.
`ORDER_FLOW_QUOTE_MAX_AGE_MS` and
`ORDER_FLOW_QUOTE_FUTURE_TOLERANCE_MS` bound quote/trade matching, and
`ORDER_FLOW_PUBLISH_THROTTLE_MS` throttles `ORDER_FLOW_BINS_UPDATE` fanout per
symbol. `ORDER_FLOW_REDIS_FLUSH_MS` throttles writes of the current
`order-flow:{symbol}:live-minute` blob, defaulting to 250ms. Closed minute
blobs are appended to `order-flow:{symbol}:minutes`.
`ORDER_FLOW_LIVE_TTL_SECONDS` keeps closed minute blobs available for today's
intraday panel until the EOD rollup has run, and
`ORDER_FLOW_LIVE_MINUTE_TTL_SECONDS` keeps the current in-progress minute fresh.
At processor startup, each pinned symbol performs one bounded live-minute read
so an unexpired in-progress aggregate can resume without losing pre-restart
trades; this read is not part of the quote/trade hot path.
`ORDER_FLOW_QUOTE_CACHE_ONLY=true` is set only on the trade/candle market
processor role when it also consumes the raw quotes topic for pinned-symbol
in-memory NBBO classification; the dedicated quote processor keeps the quote
layer topic and Redis live quote responsibility. `QUOTE_REDIS_WRITE_MIN_INTERVAL_MS`,
`QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS`, `TRADE_REDIS_WRITE_MIN_INTERVAL_MS`, and
`HEALTH_WRITE_MIN_INTERVAL_MS` throttle Redis live quote writes, quote
WebSocket fanout, live trade writes, and processor health writes respectively.
Do not raise `QUOTE_REDIS_WRITE_MIN_INTERVAL_MS` above `100` while any live
classification role still depends on Redis quote fallback.

`ACTIVE_CHART_TTL_SECONDS` keeps the symbol currently open in the chart inside
the explicit realtime cohort even when the visible chart interval is 1h, 4h,
1D, 1W, or 1M. WebSocket delivery can still be interval-specific, but trades/quotes
subscription state must not depend on whether an intraday socket is open.
`REALTIME_REDIS_POLL_SECONDS` controls the API WebSocket hub's missed-event
recovery period and defaults to five seconds. A subscription receives one Redis
snapshot, then the hub uses `market.events` pub/sub as its steady-state path and
performs batched live candle/trade/quote reads only for recovery.
Live provisional candles are Redis state and are delivered through
`market.events` pub/sub/WebSocket. There is no live-candle Kafka publication.
Optional indicator and candle-volume-profile requests run in the API with Redis
TTL cache/singleflight; there is no derived Kafka worker contract.

API-owned derived calculation tuning is committed in both env examples:

```text
CHART_INDICATOR_CACHE_TTL_SECONDS
CHART_VOLUME_PROFILE_CACHE_TTL_SECONDS
CHART_DERIVED_INLINE_LOCK_TTL_SECONDS
CHART_DERIVED_INLINE_WAIT_MS
```

These values are optional overrides. Code, Compose, and K8s all carry the same
defaults, so a missing local `.env` does not disable chart calculation.

`LIVE_CANDLE_TTL_SECONDS` and `LIVE_TRADE_TTL_SECONDS` keep Redis live state
short-lived. AWS/EKS defaults to `180` seconds so a thinly traded symbol cannot
keep showing an old premarket trade or live candle as the current price.
`LIVE_CANDLE_STALE_SECONDS`, `SYMBOL_LIVE_PRICE_STALE_SECONDS`, and
`SYMBOL_REDIS_INTRADAY_STALE_SECONDS` are read-side guards; API and WebSocket
paths ignore live values older than these thresholds even if a Redis key has not
expired yet.

Critical storage consumers may disable Kafka auto commit and commit after successful side effects:

```text
KAFKA_PROCESSOR_ENABLE_AUTO_COMMIT=false
KAFKA_S3_ENABLE_AUTO_COMMIT=false
KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT=false
KAFKA_CLICKHOUSE_MAX_POLL_RECORDS=1000
CLICKHOUSE_INSERT_BATCH_SIZE=1000
CLICKHOUSE_FLUSH_INTERVAL_SECONDS=1
CLICKHOUSE_RECENT_SOURCE_EVENT_IDS=100000
```

AWS/EKS splits ClickHouse projection consumers by topic pressure. The baseline
`alfaka-clickhouse-loader` consumes closed candle, event, and news topics only.
`alfaka-clickhouse-tick-loader` runs multiple replicas in the same
`alfaka-clickhouse-loader` consumer group and consumes
`market.layer.trades.v1,market.layer.quotes.v1`, so trade/quote tick
persistence for order-flow rollups can catch up without blocking candle/news
persistence.
The ClickHouse loader batches Kafka payloads by table while retaining each
record's topic, partition, and offset. It sends a deterministic ClickHouse
insert-deduplication token, then commits only the offsets represented by the
successful batch. A bounded recent `sourceEventId` cache catches short replays
whose records are regrouped into a different batch. Existing non-replicated
MergeTree tables must apply `scripts/local/migrate-chart-tick-retention.sql`;
without its deduplication-window setting ClickHouse does not retain insert
tokens. Keep batch sizes bounded so ClickHouse can catch up without starving API
pods.

## Redis

Current chart rebuild Redis env:

```text
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=gops:market:on-demand:v1
```

AWS/EKS may later point `REDIS_URL` at ElastiCache, Valkey, or another Redis-compatible endpoint.

For local compose and the in-cluster Redis StatefulSet, Redis is runtime
chart/live/feed-control state. It stores newest 120 confirmed candles
per `symbol + timeframe`, current provisional candles, latest closed candles,
per-interval closed watermarks that suppress stale live candles, live
trade/quote/event values, and SIP/BOATS feed state.
Durable historical candles live in ClickHouse and S3 final/manifest.
Run the in-cluster Redis StatefulSet as an ephemeral cache/control-plane store.
Do not make Redis replay large AOF/RDB files on restart; large market-data cache
snapshots can keep Redis in loading state and block Alpaca subscription control.
Historical chart data remains durable in ClickHouse and S3 final/manifest.

```text
redis-server --appendonly no --save "" --dir /tmp
```

This keeps live chart keys, feed control, and component health writable after a
pod restart. Redis restarts may drop live cache and login sessions; live chart
state is rebuilt by API/WebSocket activity. Chart resets must still use
scan-delete for the documented market-data key patterns, not `FLUSHALL`.

GOPS login sessions reuse Redis by default:

```text
AUTH_ENABLED=false
AUTH_REDIS_URL=
AUTH_REDIS_KEY_PREFIX=gops:auth
AUTH_SESSION_TTL_SECONDS=28800
AUTH_OAUTH_STATE_TTL_SECONDS=300
```

When `AUTH_REDIS_URL` is empty, the API server uses `REDIS_URL`.

## Postgres

Current local stage:

```text
docker-compose postgres
systems/order/jobs/migrations
```

Common env:

```text
DATABASE_URL
DATABASE_HOST
DATABASE_PORT
DATABASE_NAME
DATABASE_USER
DATABASE_PASSWORD
IDEMPOTENCY_HASH_SECRET
```

AWS/EKS likely uses RDS.

## KIS Demo Orders

KIS order runtime stays on mock-investment demo trading in v1:

```text
KIS_ENV=demo
KIS_CREDENTIAL_SOURCE=aws-secrets-manager
KIS_SECRET_NAME=tead/gops/kis
KIS_DEMO_APP_KEY
KIS_DEMO_APP_SECRET
KIS_DEMO_ACCOUNT_NO
KIS_ACCOUNT_PRODUCT_CODE=01
KIS_TOKEN_CACHE_PATH
KIS_TIMEOUT_SECONDS
KIS_BROKER_ADAPTER_ARGS
```

`kis-adapter` calls the KIS demo API by default. Set
`KIS_BROKER_ADAPTER_ARGS=--fake-kis success` only for an explicit local fake
smoke run. `KIS_ENV=real` remains disabled for v1.
`KIS_CREDENTIAL_SOURCE=aws-secrets-manager` reads `tead/gops/kis` by default.
Use `KIS_CREDENTIAL_SOURCE=local-env` only for an explicit direct-env smoke.

## ClickHouse

Current local stage:

```text
docker-compose clickhouse
infra/clickhouse/initdb
```

Local ClickHouse is only a development runtime. AWS ClickHouse replacement is a
post-push deployment operation: keep merge work limited to code, DDL, and env
contracts, then run AWS schema initialization and data rebuild jobs against the
new AWS endpoint.

Common env:

```text
CLICKHOUSE_HTTP_URL
CLICKHOUSE_DATABASE
CLICKHOUSE_USER
CLICKHOUSE_PASSWORD
CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS
CLICKHOUSE_PROVIDER_RETRY_ATTEMPTS
CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS
CLICKHOUSE_ENSURE_SESSION_COLUMNS
CLICKHOUSE_ENSURE_SCHEMA_ON_START
CLICKHOUSE_HTTP_TIMEOUT_SECONDS
CLICKHOUSE_REQUIRE_CANONICAL_CANDLES
```

`CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS` is the API-side read timeout for chart
serving queries. Keep it above short in-cluster ClickHouse spikes; too low a
value can turn slower intraday reads into HTTP 503 instead of a partial or
filled chart response. `CLICKHOUSE_PROVIDER_RETRY_ATTEMPTS` controls a small
API-side retry budget for transient ClickHouse timeout spikes.

Set `CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS=true` for API serving pods and `CLICKHOUSE_ENSURE_SESSION_COLUMNS=true` for storage jobs during the transition to feed/session/canonical-aware rows. New deployments create `feed_profile`, `market_session`, `price_adjustment`, and `canonical_version` in the primary schema. Existing ClickHouse volumes can add the columns idempotently, but preserving multiple feed/session rows after merges requires rebuilding old tables with the new `ORDER BY` definition. Keep `CLICKHOUSE_REQUIRE_CANONICAL_CANDLES=true` so chart serving excludes legacy/raw/unknown candles.

Keep `CLICKHOUSE_ENSURE_SCHEMA_ON_START=false` for normal API, worker, and
cache rebuild pods. Schema DDL should run as an explicit migration/maintenance
step, not from every runtime pod on rollout. `CLICKHOUSE_HTTP_TIMEOUT_SECONDS`
controls the storage client HTTP timeout used by loaders and maintenance jobs.

## S3

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

Common env:

```text
S3_BUCKET
KAFKA_RAW_S3_GROUP_ID
KAFKA_RAW_ARCHIVE_TOPICS
S3_RAW_PREFIX
S3_FINAL_PREFIX
S3_MANIFEST_PREFIX
S3_PROCESSED_FORMAT
S3_REALTIME_LAYOUT_MODE
S3_HISTORICAL_RAW_PARTITION_MODE
S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT
S3_FLUSH_COUNT
S3_FLUSH_INTERVAL_SECONDS
S3_RAW_FLUSH_COUNT
S3_RAW_FLUSH_INTERVAL_SECONDS
S3_PUT_MAX_ATTEMPTS
S3_PUT_RETRY_SLEEP_SECONDS
S3_ENDPOINT_URL
S3_MATERIALIZE_PREFIX
S3_MATERIALIZE_KEYS
S3_MATERIALIZE_MAX_OBJECTS
S3_MATERIALIZE_SYMBOL
S3_MATERIALIZE_INTERVAL
S3_MATERIALIZE_START
S3_MATERIALIZE_END
S3_MATERIALIZE_MANIFEST_PREFIX
KAFKA_S3_ENABLE_AUTO_COMMIT
KAFKA_RAW_S3_ENABLE_AUTO_COMMIT
```

Leave `S3_ENDPOINT_URL` empty for real AWS S3.

Current local/AWS chart-data prefix contract:

```text
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final
```

Keep these prefixes aligned between local Docker Compose and AWS overlays.
Pointing AWS at retired prefixes or `market-data/v2/tick-candle` mixes legacy
data with the current contract.

Local Docker services that read S3 can authenticate with direct
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` values, but the preferred local path
is `AWS_PROFILE` plus the read-only host `~/.aws` mount configured in
`docker-compose.yml` for the API and optional Alpaca ingestion
services. This keeps copied AWS keys out of `.env`. Alpaca local smoke can bypass
Secrets Manager with `ALPACA_CREDENTIAL_SOURCE=local-env`; AWS-contract runs set
`ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager`.

The processed S3 sink writes canonical layer artifacts under `S3_FINAL_PREFIX`.
The raw S3 archive sink copies only low-volume event/bar Alpaca envelopes under
`S3_RAW_PREFIX` for backup/audit. Realtime trades and quotes are excluded.

S3 prefixes have different serving roles:

```text
S3_RAW_PREFIX      backup-only Alpaca payload archive, not read by chart logic
S3_FINAL_PREFIX    deterministic canonical parquet used for ClickHouse rebuild
S3_MANIFEST_PREFIX final-object coverage evidence for on-demand fill
```

Do not configure `S3_LIVE_PREFIX` for the rebuild path. Live candles belong in
Redis/WebSocket state. Quote payloads also update Redis/WebSocket live state,
then flow through `market.layer.quotes.v1` to ClickHouse tick tables.
Processed S3 final keeps canonical candle/event artifacts. ClickHouse tick
tables, not raw S3, retain realtime trade/quote history.

Chart API reads, coverage checks, fill decisions, and ClickHouse loaders
must not query `S3_RAW_PREFIX`. S3 data becomes chart-serving data only from
final objects/manifests after a bounded materialization step writes canonical
rows into ClickHouse.

Normal market-data runtime uses `S3_PROCESSED_FORMAT=parquet`. Docker Compose
pins this value for market-data storage and API fill services so an old root `.env`
cannot silently switch runtime output back to `jsonl`.

Use time-based flush values so low-volume symbols and status events do not remain only in process memory. Keep retry settings conservative; duplicate delivery must remain safe through deterministic replay/materialization.

For S3-to-ClickHouse smoke tests, prefer `S3_MATERIALIZE_KEYS` with one or a few
explicit final candle object keys. For cold ClickHouse bootstrap from existing
S3 final evidence, leave `S3_MATERIALIZE_KEYS` empty and set
`S3_MATERIALIZE_SYMBOL`, `S3_MATERIALIZE_INTERVAL`, `S3_MATERIALIZE_START`, and
`S3_MATERIALIZE_END` so the materializer selects final objects from manifests.
Do not materialize from raw backup objects.

Sparse chart windows report bounded `coverage.gapRanges`. UI and operators
should inspect the per-request `fill` trace for those small ranges. Do not turn a
visible regular-session gap into a hidden full-range preload.

## On-Demand Fill

`GET /api/charts/candles` is the chart read entrypoint. The foreground API path
checks the requested `symbol + interval + limit/before/from/to` window in Redis
and ClickHouse first. If stored data is not renderable, the default behavior is
to return the current partial/empty payload immediately with a
`fill.backgroundFill` trace while the API process queues bounded background
repair that checks S3 final/manifest and then Alpaca historical for that same
interval/range. `ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS` allows bounded
small-window foreground repair for chart intervals such as
`1m,5m,10m,1h,4h,1D,1W,1M` even when the general foreground switch is false.
Set `ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED=true` only when all eligible small
requests should wait for direct Alpaca REST bars before responding. It does not
enqueue a Redis Stream worker and it does not run broad preload jobs from a
chart request.

```text
CHART_API_MAX_LIMIT
ON_DEMAND_FILL_BACKGROUND_ENABLED
ON_DEMAND_FILL_BACKGROUND_WORKERS
ON_DEMAND_FILL_BACKGROUND_TIMEOUT_SECONDS
ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED
ON_DEMAND_FILL_FOREGROUND_MAX_BARS
ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS
ON_DEMAND_FILL_FOREGROUND_AUTO_MAX_BARS
ON_DEMAND_FILL_TIMEOUT_SECONDS
ON_DEMAND_FILL_DISTRIBUTED_SINGLEFLIGHT_ENABLED
ON_DEMAND_FILL_SINGLEFLIGHT_LOCK_TTL_SECONDS
ON_DEMAND_FILL_SINGLEFLIGHT_TERMINAL_TTL_SECONDS
HISTORICAL_ADJUSTMENT
ALLOW_NON_CANONICAL_HISTORICAL_ADJUSTMENT
HISTORICAL_1M_MINUTES_PER_TRADING_DAY
HISTORICAL_MAX_RETRIES
HISTORICAL_RETRY_SLEEP_SECONDS
HISTORICAL_RETRY_MAX_SLEEP_SECONDS
S3_REQUIRE_CANONICAL_PROCESSED_CANDLES
DAILY_BAR_1M_REPAIR_ENABLED
DAILY_BAR_1M_REPAIR_RATIO
```

Distributed singleflight defaults to enabled. Its owner lock lives for 120
seconds and terminal state for 300 seconds; compare-and-mutate Lua prevents an
expired owner from deleting or overwriting a replacement owner's lock. Root and
backend-local `.env.example` files carry these defaults, and Compose forwards
all supported overrides. Role safety settings such as Kafka auto-commit and
`ORDER_FLOW_QUOTE_CACHE_ONLY` remain pinned in committed manifests instead of
being global `.env` knobs.

Canonical Alpaca historical fill uses `adjustment=split` and writes
`priceAdjustment=split`, `canonicalVersion=v2`. Historical fill now uses direct
Alpaca REST timeframes for every canonical interval: `1Min`, `5Min`, `10Min`,
`1Hour`, `4Hour`, `1Day`, `1Week`, and `1Month`. Realtime live/provisional
candles are still locally aggregated from live source bars where needed.
Intraday equity historical fill is session-routed before calling Alpaca REST:
`pre`, `regular`, and `after` ranges are fetched from the configured historical
feed, while `overnight` ranges are marked as BOATS-only and are not fetched
through the SIP historical path. The per-request `fill.feedRoutes` trace shows
which sub-ranges were `fetchable` and which were skipped because they require
the live/on-demand BOATS subscription path.
ClickHouse serving prefers stored direct interval rows and falls back to
query-time aggregation from `1m` or `1D` only when direct rows are missing.
Stored `1m` serving includes `priceAdjustment=live` closed realtime bars in
addition to `split`; `1D` and historical canonical materialization remain
`split` only. Raw S3 backup objects are not a fill source. Retry settings are
used for transient Alpaca historical API failures such as rate limits and 5xx
responses.
Deprecated `POST /api/charts/backfill`,
`GET /api/charts/backfill/status`, and `GET /api/charts/backfill/queue` return
`410 Gone`.

## Chart Compare

`GET /api/charts/compare` is a REST-only multi-symbol comparison path. It is not
an active chart session and must not add WebSocket clients, Redis live-candle
readers, Kafka subscriptions, or tick fanout load. The frontend sends a bounded
symbol list and a range; the API fetches Alpaca historical bars server-side,
normalizes each symbol to first-close return percent, and caches the projection
in Redis.

```text
1D -> Alpaca 1Min bars, latest regular-session trading day
1M -> Alpaca 1Hour bars
6M -> Alpaca 1Day bars
1Y -> Alpaca 1Day bars
5Y -> Alpaca 1Week bars
```

```text
CHART_COMPARE_CACHE_ENABLED
CHART_COMPARE_MAX_SYMBOLS
CHART_COMPARE_CACHE_TTL_1D_SECONDS
CHART_COMPARE_CACHE_TTL_1M_SECONDS
CHART_COMPARE_CACHE_TTL_6M_SECONDS
CHART_COMPARE_CACHE_TTL_1Y_SECONDS
CHART_COMPARE_CACHE_TTL_5Y_SECONDS
```

Alpaca credentials stay server-side and use the same credential source as the
historical fill path (`ALPACA_CREDENTIAL_SOURCE`, `ALPACA_SECRET_NAME`, or local
APCA env vars). The frontend never calls Alpaca directly.

## Market Heatmap

`GET /api/market/heatmap?universe=sp500` builds a serving projection for the
frontend TreeMap. It does not collect SEC or financial statement data directly.
The API reads the SEC fundamentals store created by `systems/fundamentals`
first: Redis key `gops:fundamentals:summary:v1:{SYMBOL}`, then ClickHouse
`sec_financial_facts` plus `sec_company_tickers`. Optional URL/file adapters are
compatibility fallbacks. The heatmap combines `sharesOutstanding` with the
latest market price from Redis/ClickHouse serving state and falls back to the
local S&P500 seed when fundamentals or quotes are missing.

```text
FUNDAMENTALS_SOURCE
FUNDAMENTALS_LATEST_FILE
FUNDAMENTALS_LATEST_URL
FUNDAMENTALS_TIMEOUT_SECONDS
HEATMAP_UNIVERSE
HEATMAP_UNIVERSE_REGISTRY_PATH
HEATMAP_QUOTE_REFRESH_SECONDS
HEATMAP_LAYOUT_REFRESH_SECONDS
HEATMAP_CACHE_TTL_SECONDS
HEATMAP_STALE_CACHE_TTL_SECONDS
```

The fundamentals store must expose `shares_outstanding` in the summary metrics
or in `sec_financial_facts`. `companyName`, `sector`, `industry`, `cik`,
`periodEndDate`, and `filedAt` are passed through when available; the S&P500
seed fills missing classification fields. Quotes, color, and computed market cap
refresh every 60 seconds by default. Tile layout timestamps advance every 300
seconds by default, so the frontend can update colors frequently without
reshuffling the treemap on every quote refresh.

SEC actuals and Yahoo consensus estimates stay separate. SEC EDGAR actual
financial statement rows live in `market_data.sec_financial_facts` and
`market_data.sec_derived_metrics`; Yahoo/yfinance consensus rows are materialized
by a separate scheduled collector into `market_data.yahoo_earnings_estimates`.
The AWS collector is `alfaka-yahoo-estimates-sync` and runs the
`systems/fundamentals/jobs/yahoo-estimates-sync/main.py` entrypoint in the
`gops-market-storage` image. It writes only Yahoo consensus rows and keeps them
separate from SEC actuals. Daily refreshes use the ClickHouse key
`symbol + metric + fiscal_year + fiscal_period + period_end`, so current
consensus values are replaced rather than duplicated indefinitely.
The API only reads ClickHouse/Redis snapshots on screen requests. It must not
call SEC or Yahoo directly from the frontend hot path.

## Market Indices

`GET /api/market/indices` serves the frontend index panel from a Redis-backed
Yahoo Finance snapshot. API pods can also warm this cache in the background so
the first user opening the panel does not trigger the only refresh path.
Multiple API pods share the same Redis refresh lock, so EKS replicas do not
fan out duplicate Yahoo Finance requests.

```text
MARKET_INDICES_WARMER_ENABLED
MARKET_INDICES_WARM_INTERVAL_SECONDS
MARKET_INDICES_CACHE_TTL_SECONDS
MARKET_INDICES_STALE_CACHE_TTL_SECONDS
MARKET_INDICES_REFRESH_SECONDS
MARKET_INDICES_STALE_REFRESH_SECONDS
MARKET_INDICES_REFRESH_LOCK_SECONDS
MARKET_INDICES_PERIOD
MARKET_INDICES_INTERVAL
MARKET_INDICES_UPSTREAM_TIMEOUT_SECONDS
```

The EKS and Docker Compose defaults enable the warmer every 60 seconds, keep the
fresh Redis snapshot for 60 seconds, and keep the last successful stale snapshot
for 30 minutes. The Yahoo Finance request interval remains `5m`; the 60-second
server refresh only asks Yahoo whether newer data is available.

## Market Calendar

GapFill and chart-analysis readiness share one year-aware US equity calendar to avoid false gaps on weekends, regular holidays, exceptional full-day closures, and early closes. `MARKET_CLOSED_DATES` remains an additive emergency override. Set `MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS=false` only for a test that intentionally disables built-in rules. The v1 provider is `configured-nyse`; it is an adapter boundary that can later be replaced by a managed exchange-calendar provider. Sunday `20:00 ET` through Friday `20:00 ET` is treated as the 24/5 equity window, with BOATS active only for the `overnight` slices. Intraday chart serving keeps historical views regular-session-only and allows the currently active `pre`, `after`, or `overnight` session to appear while it is live. Intraday chart renderability treats sparse gaps as blocking only when both candles are inside the regular session; sparse extended-hours 1m bars can still render because Alpaca may only emit bars for minutes with activity.

Chart Asset build repair is trigger-only. Base/local config audits ClickHouse and S3 but keeps Alpaca disabled; the AWS overlay enables Alpaca historical repair. It creates no extra Redis key or durable log.

```text
CHART_ASSET_REPAIR_ENABLED
CHART_ASSET_REPAIR_ALPACA_ENABLED
CHART_ASSET_REPAIR_CONCURRENCY
CHART_ASSET_REPAIR_MAX_RANGES
```

```text
MARKET_CALENDAR_PROVIDER
MARKET_TIMEZONE
MARKET_OPEN_TIME
MARKET_CLOSE_TIME
MARKET_CLOSED_DATES
MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS
MARKET_EARLY_CLOSES
```

## Coverage Repair

The manual repair job audits chart API coverage and the returned on-demand fill
trace. It does not call deprecated backfill queue endpoints.

```text
COVERAGE_REPAIR_SYMBOLS
COVERAGE_REPAIR_INTERVALS
COVERAGE_REPAIR_DRY_RUN
GOPS_API_BASE_URL
```

Keep `COVERAGE_REPAIR_DRY_RUN=true` for audits. Set it to `false` only when a
non-renderable result should fail the job.

## Agent Orchestration

Current local stage:

```text
docker-compose agent-orchestrator
docker-compose agent-event-detector
docker-compose agent-notification-publisher
```

Common env:

```text
AGENT_ORCHESTRATOR_URL
AGENT_EVENT_INPUT_TOPICS
AGENT_MARKET_EVENTS_TOPIC
AGENT_ANALYSIS_REQUESTS_TOPIC
AGENT_ANALYSIS_RESULTS_TOPIC
AGENT_NOTIFICATION_DECISIONS_TOPIC
AGENT_DLQ_TOPIC
AGENT_PUBLISH_TO_KAFKA
```

`AGENT_EVENT_INPUT_TOPICS` should include `market.layer.trades.v1`, every
interval-specific `market.layer.candles.<interval>.closed.v1` topic, and
`market.layer.events.v1` if agent/event detection needs closed-candle context.

News and macro providers are staged adapters in v1. The ontology provider can
query a GraphDB repository when the GraphDB runtime is restored and reachable.

GraphDB ontology env:

```text
GRAPHDB_SPARQL_URL
GRAPHDB_REPOSITORY
AGENT_ONTOLOGY_LIMIT
GRAPHDB_TIMEOUT_SECONDS
AGENT_ONTOLOGY_ROLE_ANALYSIS_PROVIDER
AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER
```

Ontology-only analysis and final answers use deterministic GraphDB evidence by
default. Set `AGENT_ONTOLOGY_ROLE_ANALYSIS_PROVIDER=openai` or
`AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER=openai` only when the team explicitly
accepts model synthesis for ontology output.

Financial final-answer env:

```text
AGENT_FINANCIAL_FINAL_ANSWER_PROVIDER
AGENT_FINANCIAL_SYNTHESIZER_MODEL
AGENT_FINANCIAL_SYNTHESIZER_TIMEOUT_SECONDS
AGENT_FINANCIAL_FINAL_ANSWER_CACHE_ENABLED
AGENT_FINANCIAL_FINAL_ANSWER_CACHE_TTL_SECONDS
AGENT_FINANCIAL_FINAL_ANSWER_CACHE_PREFIX
```

`AGENT_FINANCIAL_FINAL_ANSWER_PROVIDER=openai` lets the final answer layer turn
precomputed SEC fundamentals snapshots into easier Korean prose. The model must
use only formatted facts and rule-based financial signals; metric calculation
and missing-value handling remain deterministic. Redis final-answer caching is
enabled by default when `REDIS_URL` is configured.

Local restore and backup artifacts:

```text
.local-artifacts/platform-backup/
.local-artifacts/postgres/
.local-artifacts/redis/
.local-artifacts/ebs-snapshots/
.local-artifacts/graphdb/graphdb-volume.tgz
```

Files under `.local-artifacts/` may contain DB data, Kafka offsets, Redis keys,
or license material and must not be committed. To restore the GraphDB archive
into the EKS PVC, place the file at the path above and run:

```sh
scripts/aws/restore-graphdb-pvc.sh --replace-pending-pvc
```

Use `--replace-pending-pvc` only for the initial broken PVC case where
`graphdb-data-graphdb-0` is `Pending` and has no `storageClassName`. In that
case the script also recreates the `graphdb` StatefulSet, because Kubernetes
does not allow in-place updates to `volumeClaimTemplates.storageClassName`. If
GraphDB has already started once on the new PVC, it may create empty runtime
files; pass `--force` only after confirming those files can be replaced by the
local archive. GraphDB pods explicitly disable
CloudWatch/OpenTelemetry auto-instrumentation annotations so this database
runtime does not receive unwanted telemetry injection.

## AWS Observability

Cost-sensitive EKS environments should not install the managed
`amazon-cloudwatch-observability` or `aws-network-flow-monitoring-agent` add-ons
by default. If Container Insights log groups already exist, keep a short
retention period such as 3 days unless the team explicitly needs longer
operational history.

## Secrets

AWS Secrets Manager names:

```text
dev/alpaca
tead/gops/kis
/gops/prod/agent-orchestrator/openai/api-key
```

`dev/alpaca` JSON:

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

`tead/gops/kis` JSON:

```json
{"KIS_DEMO_APP_KEY":"...","KIS_DEMO_APP_SECRET":"...","KIS_DEMO_ACCOUNT_NO":"..."}
```

`/gops/prod/agent-orchestrator/openai/api-key` SecretString:

```json
{"OPENAI_API_KEY":"sk-..."}
```

AWS/EKS overlays sync this value into Kubernetes Secret
`alfaka-openai-secret` key `OPENAI_API_KEY` through External Secrets.
EKS must have External Secrets Operator installed before applying overlays that
include `ExternalSecret` and `SecretStore` resources.

Do not commit `.env`, access-key CSV files, token caches, or secret values.

Google OAuth and session settings are configured on the `gops-backend` pod:

```text
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
AUTH_SESSION_SECRET
GOOGLE_OAUTH_SECRET_NAME
AUTH_PUBLIC_BASE_URL
AUTH_COOKIE_SECURE
```

When `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, or
`AUTH_SESSION_SECRET` is empty and `AUTH_ENABLED=true`, the API server can read
the missing values from AWS Secrets Manager using `GOOGLE_OAUTH_SECRET_NAME`.
The secret JSON may use the env var names directly, or Google's downloaded
OAuth shape:

```json
{"web":{"client_id":"...","client_secret":"..."},"AUTH_SESSION_SECRET":"..."}
```

`AUTH_PUBLIC_BASE_URL` must match the public origin registered in Google OAuth
redirect URIs for `/api/auth/google/callback`.

## ECR Images

Terraform and push scripts use these custom image names:

```text
gops-frontend
gops-api-server
gops-market-ingestor
gops-market-processor
gops-market-storage
gops-order-worker
gops-kis-adapter
gops-agent-orchestrator
```

## Frontend Logo Integration

The React frontend can render stock logos from Logo.dev by ticker symbol.
Because `gops-frontend` is built into static Vite assets and served by nginx,
these `VITE_*` values are build-time inputs, not Kubernetes runtime env vars:

```text
LOGODEV_PUB_KEY=
LOGODEV_SECRET_KEY=
VITE_LOGO_DEV_ATTRIBUTION=true
```

Leave `LOGODEV_PUB_KEY` empty to show local ticker monograms without calling
Logo.dev. When set, the frontend uses
`https://img.logo.dev/ticker/{SYMBOL}` directly from browser image tags.
`VITE_LOGO_DEV_ATTRIBUTION=true` keeps the visible Logo.dev attribution required
for commercial free-plan use. Set it to `false` only when the active Logo.dev
plan permits removing attribution.
`LOGODEV_SECRET_KEY` may exist in local or CI secrets for future server-side
Logo.dev operations, but this browser-rendered logo path intentionally does not
embed it in frontend assets.

GitHub Actions dev/test deploy reads the frontend publishable key from AWS
Secrets Manager secret `icon/logodev` when `frontend` is selected. Recommended
secret JSON shape:

```json
{"LOGODEV_PUB_KEY":"pk_...","LOGODEV_SECRET_KEY":"sk_..."}
```

Only `LOGODEV_PUB_KEY` is passed to the Vite build. If the AWS secret value is
rotated without code changes, run the manual deploy with `services=frontend` so
the static frontend image is rebuilt with the new key.

## Future Dependencies

Future ontology, GraphRAG, news/context ingestion, or UI composition may add GraphDB, vector indexes, vendor APIs, trace storage, or schedulers.

Do not add provider-specific secrets or managed service resources until implementation starts for that provider.
