# GOPS Architecture

This is the current repository and runtime architecture.
For placement rules, read `STRUCTURE_GUIDE.md`.

## Repository Shape

```text
apps/
  gops-frontend/
  chart-engine/

systems/
  api-server/
  market-data/
    config/
    pods/
    jobs/
    shared/
    tests/
  order/
  agent-orchestration/

platform/
  kafka/
  flink/
  redis/
  postgres/
  clickhouse/
  s3/
  secrets/

infra/
  docker/
  k8s/
  aws/
  clickhouse/
```

## Runtime Architecture

```mermaid
flowchart LR
  User["User"]

  subgraph Apps["apps"]
    Frontend["gops-frontend"]
    ChartEngine["chart-engine"]
  end

  subgraph Api["systems/api-server"]
    ApiServer["pod: api-server<br/>FastAPI chart/order/WebSocket"]
  end

  subgraph Agents["systems/agent-orchestration"]
    AgentOrch["pod: agent-orchestrator<br/>snapshot orchestration"]
    AgentWorker["pod: agent-analysis-worker<br/>queued hot analysis"]
    DeepWorker["pod: deep-analysis-worker<br/>opt-in deep analysis"]
    DeliveryGateway["pod: agent-delivery-gateway<br/>report delivery fanout"]
    GraphRefresh["job: graph-expansion-refresh"]
    AgentEval["jobs: latency/fanout/quality eval"]
    EventDetector["pod: agent-event-detector"]
    AlertPublisher["pod: agent-notification-publisher"]
    AgentShared["shared: gops_agents.*"]
  end

  subgraph Market["systems/market-data"]
    Ingestor["pod: market-ingestor"]
    Processor["pod: market-processor"]
    S3Sink["pod: processed-s3-sink"]
    RawS3Archive["pod: raw-s3-archive"]
    CHLoader["pod: clickhouse-loader"]
    Backfill["pod: backfill-worker"]
    Registry["job: symbol-registry-sync"]
    CoverageRepair["job: coverage-repair"]
    MarketShared["shared: alfaka.*"]
  end

  subgraph Order["systems/order"]
    Outbox["pod: order-outbox"]
    KISAdapter["pod: kis-adapter"]
    Migrations["job: migrations"]
    Reconciler["job: reconciler"]
    OrderShared["shared: kis_trader.*"]
  end

  subgraph Platform["platform / external"]
    Kafka["Kafka"]
    Redis["Redis"]
    Postgres["Postgres"]
    ClickHouse["ClickHouse"]
    S3["S3"]
    Secrets["Secrets Manager"]
    KIS["KIS demo API"]
  end

  User --> Frontend
  Frontend --> ChartEngine
  Frontend --> ApiServer

  ApiServer --> Redis
  ApiServer --> ClickHouse
  ApiServer --> Postgres
  ApiServer --> AgentOrch
  ApiServer --> Kafka
  Kafka --> AgentWorker
  Kafka --> DeepWorker
  AgentWorker --> Redis
  AgentWorker --> Kafka
  AgentWorker --> AgentShared
  Kafka --> DeliveryGateway
  DeliveryGateway --> Redis
  GraphRefresh --> Redis
  GraphRefresh --> ClickHouse
  ApiServer --> MarketShared
  ApiServer --> OrderShared

  Ingestor --> Kafka
  Ingestor --> Secrets
  Ingestor --> MarketShared
  Kafka --> RawS3Archive
  RawS3Archive --> S3
  Kafka --> Processor
  Kafka --> EventDetector
  Processor --> Redis
  Processor --> S3Sink
  Processor --> CHLoader
  Processor --> MarketShared
  EventDetector --> Kafka
  AgentOrch --> AgentShared
  Kafka --> AlertPublisher
  AlertPublisher --> Redis
  S3Sink --> S3
  CHLoader --> ClickHouse
  Backfill --> Redis
  Backfill --> S3
  Backfill --> ClickHouse
  Backfill --> Secrets
  Registry --> Redis
  Registry --> ClickHouse

  Migrations --> Postgres
  Postgres --> Outbox
  Outbox --> Kafka
  Kafka --> KISAdapter
  KISAdapter --> KIS
  KISAdapter --> Postgres
  KISAdapter --> Kafka
  KISAdapter --> Secrets
  Reconciler --> Postgres
  Reconciler --> KIS
```

## Platform Staging

Platform dependencies can move through stages without changing system ownership:

```text
local compose -> single pod candidate -> managed AWS candidate
```

This matters most for Kafka and Flink/stream processing.
Do not make folder structure depend on a final AWS choice before the team decides.

## Future System Candidates

Future product areas may become systems later:

```text
systems/ontology/
systems/ui-composition/
systems/news-intelligence/
systems/user-context/
```

Create them only when real code starts.
When adding one, define pods/jobs/shared/tests, image mapping, platform dependencies, and README ownership in the same change.
