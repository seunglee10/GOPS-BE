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
ALPACA_COLLECTION_SYMBOL_SOURCE=universe
ALPACA_CHANNELS=bars,updatedBars,dailyBars,statuses
ALPACA_ACTIVE_CHANNELS=trades
ALPACA_MAX_TRADE_SYMBOLS=
ALPACA_FEED_PROFILE=sip
ALPACA_FEED_PROFILES=sip,iex,boats
ALPACA_CREDENTIAL_SOURCE=aws-secrets-manager
ALPACA_SECRET_NAME=dev/alpaca
HOT_TIER_SIZE=20
HOT_TIER_FALLBACK_SCAN_LIMIT=503
```

Full-universe collection covers bars/status channels. Trade subscriptions are resolved dynamically from active chart symbols, user Watch List symbols synced to Redis through `/api/charts/watchlist`, and hot symbols. `ALPACA_SYMBOLS` is a legacy/local smoke seed and should not be treated as the frontend Watch List source of truth or the full collection universe.
Set `ALPACA_MAX_TRADE_SYMBOLS` only when an Alpaca subscription cap requires an operational limit; active chart symbols are prioritized before watchlist and hot symbols.

`ALPACA_FEED_PROFILE` selects one ingestor runtime feed (`sip`, `iex`, or `boats`). Local compose and k8s can run one ingestor per profile, and `/health/config` reports the expected profile set from `ALPACA_FEED_PROFILES`. Market-data envelopes, Redis live state, ClickHouse candle rows, API candles, and chart snapshots preserve `feedProfile` and `marketSession` so daytime and BOATS/overnight data are diagnosable instead of collapsing into an anonymous stream.

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
```

`PROCESSOR_RECOVERY_SYMBOLS` is optional. When empty, the Python market processor tries the configured Alpaca collection universe for live/provisional state recovery at startup. Set it to a CSV list to restrict recovery during local smoke tests or incident repair.

`PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED=false` by default. Keep Redis-first recovery on by default; enable ClickHouse recovery only when the processor should rebuild missing startup state from deterministic canonical `1m`/`1D` rows and ClickHouse is known healthy.

`COMPONENT_HEALTH_TTL_SECONDS` controls how long Redis keeps lightweight component freshness heartbeats such as `pipeline:health:market-processor`.

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

Set `CLICKHOUSE_PROVIDER_ENSURE_SESSION_COLUMNS=true` for API serving pods and `CLICKHOUSE_ENSURE_SESSION_COLUMNS=true` for storage/backfill jobs during the transition to feed/session-aware rows. New deployments create `feed_profile` and `market_session` in the primary schema. Existing ClickHouse volumes can add the columns idempotently, but preserving multiple feed/session rows after merges requires rebuilding old tables with the new `ORDER BY` definition.

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
S3_LIVE_PREFIX
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
```

Leave `S3_ENDPOINT_URL` empty for real AWS S3.

Local Docker services that read AWS Secrets Manager or S3 can authenticate with
direct `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` values, but the preferred
local path is `AWS_PROFILE` plus the read-only host `~/.aws` mount configured in
`docker-compose.yml` for the API, backfill, and optional Alpaca ingestion
services. This keeps copied AWS keys out of `.env`.

The processed S3 sink writes processed Kafka topics under `S3_FINAL_PREFIX` and `S3_LIVE_PREFIX`. The raw S3 archive sink writes raw Kafka topics under `S3_RAW_PREFIX` with manifest entries under `S3_MANIFEST_PREFIX`.

For broad historical preload, keep `S3_HISTORICAL_RAW_PARTITION_MODE=chunk` and `S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT=compact`. This stores one raw object and one processed manifest entry per chunk instead of creating a small S3 object per trading day.

Broad preload and normal market-data runtime use `S3_PROCESSED_FORMAT=parquet`. Docker Compose pins this value for market-data storage/backfill services so an old root `.env` cannot silently switch runtime output back to `jsonl`.

Use time-based flush values so low-volume symbols and status events do not remain only in process memory. Keep retry settings conservative; duplicate delivery must remain safe through deterministic replay/materialization.

For S3-to-ClickHouse smoke tests, prefer `S3_MATERIALIZE_KEYS` with one or a few explicit processed candle object keys. Leave it empty only for intentional prefix-wide materialization through `S3_MATERIALIZE_PREFIX`.

## Backfill Queue

Backfill requests are stored in Redis status keys and queued through Redis Streams.
Workers consume the `backfill-workers` group, reclaim idle pending jobs, and move exhausted jobs to a dead-letter stream.

```text
BACKFILL_EXECUTION_MODE
BACKFILL_STATUS_TTL_SECONDS
BACKFILL_QUEUE_BACKEND
BACKFILL_STREAM_GROUP
BACKFILL_STREAM_RECLAIM_IDLE_MS
BACKFILL_STREAM_MAXLEN
BACKFILL_MAX_ATTEMPTS
BACKFILL_GAPFILL_DETECT_INTERNAL
BACKFILL_GAPFILL_MAX_DETECT_DAYS
BACKFILL_GAPFILL_TIMESTAMP_LIMIT
BACKFILL_INITIAL_LOAD_1M_CHUNK_DAYS
BACKFILL_INITIAL_LOAD_1D_CHUNK_DAYS
BACKFILL_INITIAL_LOAD_1M_MIN_START
BACKFILL_INITIAL_LOAD_MAX_ENQUEUE
BACKFILL_INITIAL_LOAD_MAX_BACKLOG
BACKFILL_WORKER_POLL_SECONDS
BACKFILL_WORKER_ONCE
HISTORICAL_ADJUSTMENT
HISTORICAL_1M_MINUTES_PER_TRADING_DAY
HISTORICAL_MAX_RETRIES
HISTORICAL_RETRY_SLEEP_SECONDS
HISTORICAL_RETRY_MAX_SLEEP_SECONDS
```

Use `HISTORICAL_ADJUSTMENT=raw` for canonical Alpaca historical backfill unless the live/backfill adjustment policy is explicitly changed. `HISTORICAL_1M_MINUTES_PER_TRADING_DAY=960` keeps 1m preload dry-run estimates conservative for Alpaca extended-hours bars. Retry settings are used for transient Alpaca historical API failures such as rate limits and 5xx responses.

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

## Coverage Repair

The manual repair job audits chart API coverage and can queue missing source interval backfills.

```text
COVERAGE_REPAIR_SYMBOLS
COVERAGE_REPAIR_INTERVALS
COVERAGE_REPAIR_DRY_RUN
COVERAGE_REPAIR_FORCE
GOPS_API_BASE_URL
```

Keep `COVERAGE_REPAIR_DRY_RUN=true` for audits. Set it to `false` only when intentionally queuing backfills.

## Initial Load

The initial-load job plans or queues chunked `initial_load` Redis Streams jobs for canonical `1m` and `1D` history. It is dry-run by default; set `INITIAL_LOAD_DRY_RUN=false` only after reviewing the plan. Use `INITIAL_LOAD_SYMBOLS=universe` to resolve the configured S&P 500 collection registry.

For the S&P 500 bootstrap, preload `1D` for the 3-year range first, then run `1m` as repeated dry-run/review/enqueue windows. The v1 `1m` initial-load lower bound is `BACKFILL_INITIAL_LOAD_1M_MIN_START=2025-04-01T00:00:00Z`; `1m` ranges starting before April 2025 are rejected so Goal/deploy runs do not accidentally fetch the old 3-year intraday plan. The operational default for `INITIAL_LOAD_INTERVALS` is `1D`; pass `INITIAL_LOAD_INTERVALS=1m` explicitly for intraday preload windows. Processed candle output should use `S3_PROCESSED_FORMAT=parquet` for broad preload, with historical raw chunks and processed compact manifests enabled. Existing queued/running/succeeded chunks are skipped during resume and do not consume `INITIAL_LOAD_MAX_ENQUEUE` capacity.

Initial Load is a bootstrap job, not a normal chart GapFill. It should create S3 raw/processed evidence even when ClickHouse already has the requested rows; ClickHouse coverage alone is not enough to prove the S3 preload objective.

If Alpaca returns no bars for an Initial Load chunk, the worker writes an empty marker under `S3_MANIFEST_PREFIX/empty/candles/...` and completes the chunk as `alpaca-empty`. This is expected for current S&P 500 symbols that did not trade during older parts of a requested preload window.

```text
INITIAL_LOAD_SYMBOLS
INITIAL_LOAD_INTERVALS
INITIAL_LOAD_START
INITIAL_LOAD_END
INITIAL_LOAD_DRY_RUN
INITIAL_LOAD_FORCE
INITIAL_LOAD_SOURCE_PREFERENCE
INITIAL_LOAD_MAX_ENQUEUE
INITIAL_LOAD_MAX_BACKLOG
```

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
