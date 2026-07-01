# GOPS Environment And Platform Contracts

This file documents platform dependencies and env contracts.
Do not put real secrets here.

## Platform Folders

```text
platform/kafka/
platform/flink/
platform/redis/
platform/postgres/
platform/clickhouse/
platform/s3/
platform/secrets/
```

Each folder explains local behavior, possible next stages, env vars, and related compose/k8s/AWS assets.

## Alpaca Collection

Current v1 universe contract:

```text
ALFAKA_REQUEST_CONFIG=systems/market-data/config/market-data-request.json
ALPACA_UNIVERSE=sp500
ALPACA_UNIVERSE_REGISTRY_PATH=systems/market-data/config/sp500-universe.json
ALPACA_CHANNELS=bars,updatedBars,dailyBars,statuses
ALPACA_ACTIVE_CHANNELS=trades
ALPACA_MAX_TRADE_SYMBOLS=
ALPACA_MAX_WATCHLIST_TRADE_SYMBOLS=40
ALPACA_MAX_HOT_TRADE_SYMBOLS=10
ALPACA_ACTIVE_POLL_SECONDS=1
ALPACA_FEED_PROFILE=sip
ALPACA_FEED_PROFILES=sip
PIPELINE_REQUIRED_COMPONENTS=market-ingestor-sip,market-processor
MARKET_FEED_PROFILE_PRIORITY=sip,iex,boats,overnight,unknown
MARKET_OVERNIGHT_FEED_PROFILE_PRIORITY=boats,overnight,sip,iex,unknown
ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager
ALPACA_SECRET_NAME=dev/alpaca
HOT_TIER_SIZE=10
HOT_TIER_FALLBACK_SCAN_LIMIT=503
HOT_TIER_PREVIOUS_CLOSE_MAX_AGE_DAYS=10
```

Full-universe collection covers bars/status channels. Trade subscriptions are resolved dynamically from active chart symbols, user Watch List symbols synced to Redis through `/api/charts/watchlist`, and hot symbols. Active chart symbols are always prioritized, while Watch List and Hot Ranking trade tiers are capped by `ALPACA_MAX_WATCHLIST_TRADE_SYMBOLS` and `ALPACA_MAX_HOT_TRADE_SYMBOLS`. The dynamic trade subscription loop polls Redis every `ALPACA_ACTIVE_POLL_SECONDS` seconds and sends only WebSocket subscribe/unsubscribe diffs. Set either tier cap to `0` to disable that tier's trade subscription. Set `ALPACA_MAX_TRADE_SYMBOLS` only when an Alpaca account-level subscription cap requires one final emergency limit after tier ordering.

`HOT_TIER_PREVIOUS_CLOSE_MAX_AGE_DAYS` prevents stale historical daily candles from being used as the Hot Ranking `changePercent` baseline. If no recent previous daily or intraday close exists, the API returns `changePercent: null` rather than a misleading multi-year percentage move.

`ALPACA_FEED_PROFILE` selects one ingestor runtime feed (`sip`, `iex`, or `boats`). The default local compose and k8s runtime starts only the SIP ingestor to avoid Alpaca account-level WebSocket connection limits. Enable IEX or BOATS only through their explicit optional compose profiles or the optional k8s manifest at `infra/k8s/optional/deployment-alpaca-ingestor-extra-feeds.yaml`, and then add those profile names to `ALPACA_FEED_PROFILES` so `/health/config` reports them. Market-data envelopes, Redis live state, ClickHouse candle rows, API candles, and chart snapshots preserve `feedProfile` and `marketSession`. Chart reads choose a canonical series with `MARKET_FEED_PROFILE_PRIORITY`; overnight rows use `MARKET_OVERNIGHT_FEED_PROFILE_PRIORITY`.

`PIPELINE_REQUIRED_COMPONENTS` controls which Redis component heartbeats make `/health/config` warn. Deployed runtimes require `market-ingestor-sip,market-processor`. Local compose defaults to `market-processor` because Alpaca ingestors are opt-in profile services; set `PIPELINE_REQUIRED_COMPONENTS=market-ingestor-sip,market-processor` when running `docker compose --profile alpaca up`.

`ALPACA_CREDENTIAL_SOURCE` accepts `auto`, `aws-secrets-manager`, or `local-env`. Local AWS-contract and Docker Compose market-data services pin `aws-secrets-manager` so stale local `APCA_*` values cannot override Secrets Manager credentials. Use `local-env` only for an explicit local smoke outside the AWS-contract flow.

## Kafka

Current local stage:

```text
docker-compose kafka
docker-compose kafka-init
platform/kafka/topics.txt
```

Topic contract:

```text
market.raw.bars
market.raw.updated-bars
market.raw.trades
market.raw.daily-bars
market.raw.statuses
market.raw.quotes
market.raw.corrections
market.raw.cancel-errors
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
market.status.v1
market.volume-profile-bins.1m.v1
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

## Flink / Stream Processing

Current repository stage:

```text
systems/market-data/pods/market-processor/local_main.py
infra/k8s/base/deployment-market-processor.yaml
systems/market-data/pods/market-processor/flink/market-data-normalizer
```

Staged path:

```text
local Python processor -> explicit Python processor pod -> Flink or managed Flink candidate
```

Common stream processor env:

```text
KAFKA_BOOTSTRAP_SERVERS
KAFKA_RAW_TOPIC_PREFIX
KAFKA_PROCESSOR_GROUP_ID
REDIS_URL
PROCESSOR_RECOVERY_SYMBOLS
PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED
COMPONENT_HEALTH_TTL_SECONDS
PIPELINE_REQUIRED_COMPONENTS
```

`PROCESSOR_RECOVERY_SYMBOLS` is optional. When empty, the Python market processor tries the configured Alpaca collection universe for live/provisional state recovery at startup. Set it to a CSV list to restrict recovery during local smoke tests or incident repair.

`PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED=false` by default. Keep Redis-first recovery on by default; enable ClickHouse recovery only when the processor should rebuild missing startup state from deterministic canonical `1m`/`1D` rows and ClickHouse is known healthy.

`COMPONENT_HEALTH_TTL_SECONDS` controls how long Redis keeps lightweight component freshness heartbeats such as `pipeline:health:market-processor`.

`PIPELINE_REQUIRED_COMPONENTS` is a comma-separated list of heartbeat component names that `/health/config` treats as required.

Critical storage consumers may disable Kafka auto commit and commit after successful side effects:

```text
KAFKA_CLICKHOUSE_ENABLE_AUTO_COMMIT=false
```

## Redis

Current local env:

```text
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=
```

AWS/EKS may later point `REDIS_URL` at ElastiCache, Valkey, or another Redis-compatible endpoint.

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
CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS
CLICKHOUSE_ENSURE_SESSION_COLUMNS
```

Set `CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS=true` for API serving pods and `CLICKHOUSE_ENSURE_SESSION_COLUMNS=true` for storage/backfill jobs. New deployments create `feed_profile` and `market_session` in the primary schema. Existing ClickHouse volumes can add the columns idempotently, but preserving multiple feed/session rows after merges requires rebuilding tables with the feed/session-aware `ORDER BY` definition.

## S3 Archive Utilities

S3 archive utilities are not part of chart serving or online backfill/gapfill reads. The chart path serves Redis/ClickHouse, and backfill/gapfill fetches missing Alpaca ranges, materializes them directly into ClickHouse, then best-effort archives processed candles to S3 when archive env is present.

Current optional AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

Common env:

```text
S3_BUCKET
S3_ARCHIVE_ROOT_PREFIX
S3_FINAL_PREFIX
S3_MANIFEST_PREFIX
S3_BACKFILL_PROCESSED_PREFIX
S3_BACKFILL_MANIFEST_PREFIX
S3_BACKFILL_PROCESSED_FORMAT
S3_BACKFILL_PROCESSED_MANIFEST_LAYOUT
S3_BACKFILL_ARCHIVE_ROWS_PER_OBJECT
BACKFILL_S3_ARCHIVE_ENABLED
S3_PROCESSED_FORMAT
CLICKHOUSE_LOADER_S3_ARCHIVE_ENABLED
S3_CLICKHOUSE_ARCHIVE_FLUSH_ROWS
S3_CLICKHOUSE_ARCHIVE_FLUSH_SECONDS
S3_CLICKHOUSE_ARCHIVE_ROWS_PER_OBJECT
S3_PUT_MAX_ATTEMPTS
S3_PUT_RETRY_SLEEP_SECONDS
S3_ENDPOINT_URL
```

Leave `S3_ENDPOINT_URL` empty for real AWS S3.

Default development prefixes are isolated under `market-data/dev/helixho/...`. Use `S3_ARCHIVE_ROOT_PREFIX` or the specific processed/backfill prefix envs to move the namespace intentionally.

Local Docker services that read AWS Secrets Manager or S3 can authenticate with
direct `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` values, but the preferred
local path is `AWS_PROFILE` plus the read-only host `~/.aws` mount configured in
`docker-compose.yml` for the API, backfill, and optional Alpaca ingestion
services. This keeps copied AWS keys out of `.env`.

The ClickHouse loader can best-effort archive closed candles under `S3_FINAL_PREFIX` after the ClickHouse insert succeeds. Backfill workers similarly archive accepted historical candles under `S3_BACKFILL_PROCESSED_PREFIX`. Raw Alpaca Kafka topics are not archived by default; trade ticks remain realtime-only and are not persisted.

Backfill workers do not require `S3_BUCKET` and never read S3 candle data as an input. `BACKFILL_S3_ARCHIVE_ENABLED=false` disables the optional post-write archive without changing ClickHouse materialization. When `S3_BUCKET` is configured and archive is enabled, they archive only the processed candles that were already accepted for ClickHouse under `S3_BACKFILL_PROCESSED_PREFIX`; S3 archive failures are recorded in the backfill result but do not fail ClickHouse materialization or chart rendering. `S3_BACKFILL_MANIFEST_PREFIX` overrides the common manifest prefix for backfill archive manifests. `S3_BACKFILL_ARCHIVE_ROWS_PER_OBJECT` keeps large historical backfills in page-sized archive objects instead of one S3 object per candle.

ClickHouse-loader post-insert archive is controlled by `CLICKHOUSE_LOADER_S3_ARCHIVE_ENABLED` and uses `S3_PROCESSED_FORMAT=parquet` by default. It buffers accepted candles with `S3_CLICKHOUSE_ARCHIVE_FLUSH_ROWS` / `S3_CLICKHOUSE_ARCHIVE_FLUSH_SECONDS` so S&P 500 realtime bars do not create one S3 object per candle. Backfill post-write archive defaults to `S3_BACKFILL_PROCESSED_FORMAT=jsonl` so the backfill worker does not require a parquet runtime. There is no Kafka-to-S3 runtime worker; archive writes happen only after ClickHouse accepts rows.

Keep retry settings conservative; duplicate delivery must remain safe through deterministic replay/materialization.

## Backfill Queue

Backfill requests are stored in Redis status keys and queued through Redis Streams.
Workers consume the `backfill-workers` group, reclaim idle pending jobs, and move exhausted jobs to a dead-letter stream.

```text
BACKFILL_STATUS_TTL_SECONDS
BACKFILL_QUEUE_BACKEND
BACKFILL_STREAM_GROUP
BACKFILL_STREAM_RECLAIM_IDLE_MS
BACKFILL_STREAM_MAXLEN
BACKFILL_MAX_ATTEMPTS
BACKFILL_GAPFILL_DETECT_INTERNAL
BACKFILL_GAPFILL_MAX_DETECT_DAYS_INTRADAY
BACKFILL_GAPFILL_MAX_DETECT_DAYS_DAILY
BACKFILL_GAPFILL_TIMESTAMP_LIMIT
BACKFILL_DAILY_FETCH_MAX_DAYS
BACKFILL_INTRADAY_FETCH_MAX_DAYS
BACKFILL_WORKER_POLL_SECONDS
HISTORICAL_LIMIT
HISTORICAL_ADJUSTMENT
MARKET_DATA_MAX_HISTORY_YEARS
HISTORICAL_MAX_RETRIES
HISTORICAL_RETRY_SLEEP_SECONDS
HISTORICAL_RETRY_MAX_SLEEP_SECONDS
```

Use `HISTORICAL_ADJUSTMENT=split` for canonical Alpaca historical backfill so split events do not create discontinuous higher-timeframe candles. `MARKET_DATA_MAX_HISTORY_YEARS` defaults to `6`; the API serving path ignores older stored candles and the backfill worker clamps Alpaca fetches to that window. A `force=true` backfill still respects that window, but bypasses ClickHouse coverage skips and refetches the whole clamped range. Retry settings are used for transient Alpaca historical API failures such as rate limits and 5xx responses.

`HISTORICAL_LIMIT` defaults to `10000`, the Alpaca historical bars page size used by bounded backfill jobs. The worker follows `next_page_token` until the requested range is exhausted. Keep online backfill jobs symbol-scoped; do not satisfy a user chart range by issuing one oversized S&P 500 historical request, because Alpaca paginates by response data points and that can starve later symbols.

`BACKFILL_GAPFILL_MAX_DETECT_DAYS_INTRADAY` bounds each ClickHouse timestamp scan for minute-derived intervals. `BACKFILL_GAPFILL_MAX_DETECT_DAYS_DAILY` is intentionally much larger because `1D` is the stored source for daily, weekly, and monthly chart backfill. `BACKFILL_DAILY_FETCH_MAX_DAYS` controls how broadly daily Alpaca repair ranges are merged before fetching; `BACKFILL_INTRADAY_FETCH_MAX_DAYS` does the same for intraday Alpaca fetch ranges and defaults to a page-sized 30 calendar days. Both fetch settings are separate from ClickHouse gap scan chunking.

## Market Calendar

GapFill uses the configured market calendar to avoid false gaps on weekends, holidays, and early closes. The v1 provider is `configured-nyse`; it is an adapter boundary that can later be replaced by a managed exchange-calendar provider. Intraday chart renderability treats sparse gaps as blocking only when both candles are inside the regular session; sparse extended-hours 1m bars can still render because Alpaca may only emit bars for minutes with activity.

```text
MARKET_CALENDAR_PROVIDER
MARKET_TIMEZONE
MARKET_OPEN_TIME
MARKET_CLOSE_TIME
MARKET_CLOSED_DATES
MARKET_EARLY_CLOSES
```

`configured-nyse` includes standard NYSE holidays; `MARKET_CLOSED_DATES` and `MARKET_EARLY_CLOSES` are for one-off closures or overrides. Chart backfill is range-driven. The frontend requests the visible range plus an interval-specific buffer, using regular-session market minutes and the default NYSE holiday calendar for intraday intervals so pan/zoom across overnight, weekend, or holiday gaps still reaches the prior tradable candles. `1m`, `5m`, `10m`, and `1D` use a larger buffer than higher timeframes to reduce pan jitter while still staying tied to the user's visible range. A small source-bucket floor prevents tiny zoomed-in requests, but it must stay near the default visible chart size rather than becoming a hidden preload window. The candles API uses half-open `from`/`to` bounds for these buffered history reads; if the range is missing, the backfill API queues that exact half-open `start`/`end` range (`[start, end)`), and the worker fetches only missing ClickHouse buckets from Alpaca. Daily source candles are stored and compared at canonical UTC date midnight (`YYYY-MM-DDT00:00:00.000Z`); weekly and monthly charts derive from that `1D` source. Weekly and monthly reads filter returned rows by the derived bucket timestamp, not by slicing daily source rows directly, so callers do not see partial higher-timeframe candles when `from` or `to` is inside a week or month. Chart reads, coverage checks, and gap detection all use the same half-open boundary contract. If Alpaca returns no bars, or only returns a later leading-edge partial history for a range that should contain market buckets, the worker records a no-data boundary so the chart stops retrying that unavailable range.

If an existing ClickHouse volume still contains legacy daily rows at `04:00` or `05:00` UTC, run `PYTHONPATH=systems/market-data/shared python -m alfaka.tools.repair_daily_candle_timestamps` for a dry-run summary. Add `--apply --wait` only when the reported rows should be rewritten to canonical UTC-midnight daily candles.

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
gops-backfill-worker
gops-order-worker
gops-kis-adapter
gops-agent-orchestrator
```

## Future Dependencies

Future ontology, GraphRAG, news/context ingestion, or UI composition may add GraphDB, vector indexes, vendor APIs, trace storage, or schedulers.

Do not add provider-specific secrets or managed service resources until implementation starts for that provider.
