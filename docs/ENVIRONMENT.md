# GOPS Environment And Platform Contracts

This file documents platform dependencies and env contracts.
Do not put real secrets here.

For chart-data work, `docs/CHART_DATA_REBUILD_PLAN.md` is the source of truth.
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
ALPACA_MAX_TRADE_SYMBOLS=
ALPACA_FEED_PROFILE=sip
ALPACA_FEED_PROFILES=sip,boats,crypto-us
ALPACA_CRYPTO_LOCATION=us
ALPACA_CRYPTO_SYMBOLS=BTCUSD
ALPACA_CRYPTO_CHANNELS=bars,updatedBars,dailyBars,trades,quotes
ALPACA_ENFORCE_FEED_SESSION_WINDOW=true
ALPACA_SESSION_IDLE_POLL_SECONDS=60
ALPACA_CREDENTIAL_SOURCE=local-env
ALPACA_SECRET_NAME=
HOT_TIER_SIZE=10
HOT_TIER_FALLBACK_SCAN_LIMIT=20
```

S&P500 baseline collection subscribes to `bars`, `updatedBars`, `dailyBars`, and
`statuses` only. It does not subscribe every S&P500 symbol to high-frequency
`trades` or `quotes`. `trades` and `quotes` follow the exact same explicit
symbol set as realtime cohorts: watchlist, portfolio, rankings, active chart
sessions, and manual admin subscriptions. Quotes are never a separate all-symbol
feed.
Set `ALPACA_MAX_TRADE_SYMBOLS` only when an Alpaca subscription cap requires an
operational limit; explicit active chart subscriptions remain the priority.

`ALPACA_FEED_PROFILE` selects one ingestor runtime feed (`sip`, `boats`, or `crypto-us`). The live contract is session-routed: SIP is primary for `04:00-20:00 ET` (`pre`, `regular`, `after`), BOATS is primary for `20:00-04:00 ET` (`overnight`), and `crypto-us` is the 24/7 Alpaca crypto feed. Local compose and k8s run one ingestor per active profile, and `/health/config` reports the expected profile set from `ALPACA_FEED_PROFILES`. Market-data envelopes, Redis live state, ClickHouse candle rows, API candles, and chart snapshots preserve `feedProfile` and `marketSession` so daytime, BOATS/overnight, and crypto data are diagnosable instead of collapsing into an anonymous stream.

Crypto uses `BTCUSD` inside GOPS and `BTC/USD` only when talking to Alpaca. It shares the existing Kafka topics; records are separated by Kafka key/message `symbol=BTCUSD`, not by a new topic.

`ALPACA_CREDENTIAL_SOURCE` accepts `auto`, `aws-secrets-manager`, or `local-env`. Use `local-env` with `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` for explicit local Alpaca smoke runs while Secrets Manager is disconnected. AWS/EKS overlays set `aws-secrets-manager` and read the same canonical key names from the `dev/alpaca` JSON secret.

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
market.layer.candles.live.v1
market.layer.candles.closed.v1
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

Do not force MSK as the next step. The staged path is:

```text
local compose -> single Kafka pod candidate -> MSK candidate
```

## Python / Kubernetes Stream Processing

Current repository stage:

```text
systems/market-data/pods/market-processor/local_main.py
infra/k8s/base/app/deployment-market-processor.yaml
```

Runtime path:

```text
local Python processor -> explicit Kubernetes processor pod
```

Common stream processor env:

```text
KAFKA_BOOTSTRAP_SERVERS
KAFKA_INPUT_TOPIC_PREFIX
KAFKA_PROCESSOR_GROUP_ID
KAFKA_PROCESSOR_ENABLE_AUTO_COMMIT
CANDLE_WATERMARK_GRACE_SECONDS
CANDLE_FLUSH_INTERVAL_SECONDS
REDIS_URL
PROCESSOR_RECOVERY_SYMBOLS
PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED
COMPONENT_HEALTH_TTL_SECONDS
```

`PROCESSOR_RECOVERY_SYMBOLS` is optional. Keep it empty unless an incident repair
needs explicit symbols. Baseline S&P500 collection is performed by the ingestor;
processor recovery should still avoid broad ClickHouse recovery unless an
operator explicitly enables it.

`PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED=false` by default. Keep Redis-first recovery on by default; enable ClickHouse recovery only when the processor should rebuild missing startup state from deterministic canonical `1m`/`1D` rows and ClickHouse is known healthy.

`COMPONENT_HEALTH_TTL_SECONDS` controls how long Redis keeps lightweight component freshness heartbeats such as `pipeline:health:market-processor`.

Critical storage consumers may disable Kafka auto commit and commit after successful side effects:

```text
KAFKA_PROCESSOR_ENABLE_AUTO_COMMIT=false
KAFKA_S3_ENABLE_AUTO_COMMIT=false
KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT=false
```

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
live trade/quote/event values, and SIP/BOATS feed state.
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
CLICKHOUSE_REQUIRE_CANONICAL_CANDLES
```

`CLICKHOUSE_PROVIDER_TIMEOUT_SECONDS` is the API-side read timeout for chart
serving queries. Keep it above short in-cluster ClickHouse spikes; too low a
value can turn slower intraday reads into HTTP 503 instead of a partial or
filled chart response. `CLICKHOUSE_PROVIDER_RETRY_ATTEMPTS` controls a small
API-side retry budget for transient ClickHouse timeout spikes.

Set `CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS=true` for API serving pods and `CLICKHOUSE_ENSURE_SESSION_COLUMNS=true` for storage jobs during the transition to feed/session/canonical-aware rows. New deployments create `feed_profile`, `market_session`, `price_adjustment`, and `canonical_version` in the primary schema. Existing ClickHouse volumes can add the columns idempotently, but preserving multiple feed/session rows after merges requires rebuilding old tables with the new `ORDER BY` definition. Keep `CLICKHOUSE_REQUIRE_CANONICAL_CANDLES=true` so chart serving excludes legacy/raw/unknown candles.

## S3

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

Common env:

```text
S3_BUCKET
KAFKA_RAW_S3_GROUP_ID
S3_RAW_PREFIX
S3_FINAL_PREFIX
S3_MANIFEST_PREFIX
S3_PROCESSED_FORMAT
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
```

Leave `S3_ENDPOINT_URL` empty for real AWS S3.

Current local/AWS chart rebuild prefix contract:

```text
S3_RAW_PREFIX=market-data/rebuild-20260702-lazy-v1/raw/alpaca
S3_FINAL_PREFIX=market-data/rebuild-20260702-lazy-v1/final
S3_MANIFEST_PREFIX=market-data/rebuild-20260702-lazy-v1/manifest
S3_MATERIALIZE_PREFIX=market-data/rebuild-20260702-lazy-v1/final
```

Keep these prefixes aligned between local Docker Compose and AWS overlays while
the on-demand chart rebuild is active. Pointing AWS at old dated rebuild
prefixes or `market-data/v2/tick-candle` mixes legacy data with the new
contract.

Local Docker services that read S3 can authenticate with direct
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` values, but the preferred local path
is `AWS_PROFILE` plus the read-only host `~/.aws` mount configured in
`docker-compose.yml` for the API and optional Alpaca ingestion
services. This keeps copied AWS keys out of `.env`. Alpaca local smoke can bypass
Secrets Manager with `ALPACA_CREDENTIAL_SOURCE=local-env`; AWS-contract runs set
`ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager`.

The processed S3 sink writes canonical layer artifacts under `S3_FINAL_PREFIX`.
The raw S3 archive sink may copy Alpaca payload envelopes under `S3_RAW_PREFIX`
for backup/audit only.

S3 prefixes have different serving roles:

```text
S3_RAW_PREFIX      backup-only Alpaca payload archive, not read by chart logic
S3_FINAL_PREFIX    deterministic canonical parquet used for ClickHouse rebuild
S3_MANIFEST_PREFIX final-object coverage evidence for on-demand fill
```

Do not configure `S3_LIVE_PREFIX` for the rebuild path. Live candles belong in
Redis/WebSocket state. Quote payloads also update Redis/WebSocket live state,
then flow through `market.layer.quotes.v1` to S3 final and ClickHouse.

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

`GET /api/charts/candles` is the chart read and fill entrypoint. The API checks
the requested `symbol + interval + limit/before/from/to` window in order:
Redis, ClickHouse, S3 final/manifest, then Alpaca historical. It does not enqueue
a Redis Stream worker and it does not run broad preload jobs from a chart request.

```text
ON_DEMAND_FILL_TIMEOUT_SECONDS
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

Canonical Alpaca historical fill uses `adjustment=split` and writes
`priceAdjustment=split`, `canonicalVersion=v2`. `5m` and `10m` fill through
`1m` source bars; `1W` and `1M` fill through `1D` source bars and are then
aggregated for serving. Raw S3 backup objects are not a fill source. Retry
settings are used for transient Alpaca historical API failures such as rate
limits and 5xx responses. Deprecated `POST /api/charts/backfill`,
`GET /api/charts/backfill/status`, and `GET /api/charts/backfill/queue` return
`410 Gone`.

## Market Calendar

GapFill uses the configured market calendar to avoid false gaps on weekends, holidays, and early closes. Alpaca feed session gating reads `MARKET_CLOSED_DATES` plus the built-in 2026 NYSE/Nasdaq full-day holiday set by default; a full-market holiday reports `closed` instead of `pre`, `regular`, `after`, or `overnight`, so local smoke tests do not wait for live payloads on a known closed session. Set `MARKET_INCLUDE_DEFAULT_US_EQUITY_HOLIDAYS=false` only for a test that intentionally disables the built-in holiday set. The v1 provider is `configured-nyse`; it is an adapter boundary that can later be replaced by a managed exchange-calendar provider. Intraday chart renderability treats sparse gaps as blocking only when both candles are inside the regular session; sparse extended-hours 1m bars can still render because Alpaca may only emit bars for minutes with activity.

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

Local GraphDB restore artifact:

```text
.local-artifacts/graphdb/graphdb-volume.tgz
```

`graphdb-volume.tgz` is a local restore artifact and must not be committed. To
restore it into the EKS PVC, place the file at the path above and run:

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

## Future Dependencies

Future ontology, GraphRAG, news/context ingestion, or UI composition may add GraphDB, vector indexes, vendor APIs, trace storage, or schedulers.

Do not add provider-specific secrets or managed service resources until implementation starts for that provider.
