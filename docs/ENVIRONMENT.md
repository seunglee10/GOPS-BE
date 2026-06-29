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
```

Do not force MSK as the next step. The staged path is:

```text
local compose -> single Kafka pod candidate -> MSK candidate
```

## Flink / Stream Processing

Current local stage:

```text
systems/market-data/pods/market-processor/local_main.py
systems/market-data/pods/market-processor/flink/market-data-normalizer
```

Staged path:

```text
local Python processor -> single processor pod candidate -> Flink or managed Flink candidate
```

## Redis

Current local env:

```text
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=
```

AWS/EKS may later point `REDIS_URL` at ElastiCache, Valkey, or another Redis-compatible endpoint.

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
```

## S3

Current AWS bucket:

```text
gops-market-data-<aws-account-id>-ap-northeast-2-an
```

Common env:

```text
S3_BUCKET
S3_RAW_PREFIX
S3_FINAL_PREFIX
S3_LIVE_PREFIX
S3_PROCESSED_FORMAT
S3_ENDPOINT_URL
```

Leave `S3_ENDPOINT_URL` empty for real AWS S3.

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

## Secrets

AWS Secrets Manager names:

```text
dev/alpaca
dev/kis
```

`dev/alpaca` JSON:

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

`dev/kis` JSON:

```json
{"KIS_DEMO_APP_KEY":"...","KIS_DEMO_APP_SECRET":"...","KIS_DEMO_ACCOUNT_NO":"..."}
```

Do not commit `.env`, access-key CSV files, token caches, or secret values.

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
```

## Future Dependencies

Future ontology, GraphRAG, multi-agent analysis, news/context ingestion, or UI composition may add GraphDB, vector indexes, LLM provider secrets, vendor APIs, trace storage, or schedulers.

Do not add env vars, compose services, k8s manifests, or Terraform resources for future dependencies until implementation starts.
