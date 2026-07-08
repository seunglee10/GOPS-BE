# GOPS Architecture

This is the current repository and runtime architecture.
For placement rules, read `STRUCTURE_GUIDE.md`.

Chart-data rebuild work must use `CHART_DATA_REBUILD_PLAN.md` as the
source-of-truth. Older market-data notes that describe preset historical
universe preload, S&P500-wide tick/quote collection, non-Mermaid Kafka topic
layouts, raw S3 replay as an active read path, or Redis as historical storage
are superseded.

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
    AgentOrch["pod: agent-orchestrator<br/>role agents"]
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

## Chart Data Rebuild Boundary

The chart rebuild is on-demand:

```text
Frontend chart request
  -> API Redis latest 120 check
  -> ClickHouse confirmed history
  -> bounded auto/general foreground Alpaca REST direct bars
  -> background S3 final/manifest evidence
  -> background Alpaca historical direct fill for the requested interval/range
```

Realtime data is feed-guarded and symbol-keyed:

```text
SIP only 04:00-20:00 ET / BOATS only 20:00-04:00 ET
  -> Kafka market.input.realtime.* topics with key=symbol
  -> processor feed guard
  -> Redis live/provisional/latest 120
  -> market.layer.* topics for canonical downstream storage
```

Raw Alpaca payload archives may be written to S3 for backup only. Raw archives
must not participate in chart serving, coverage checks, fill decisions, or
ClickHouse loading unless a future explicit raw-replay pipeline is designed.

## Platform Staging

Platform dependencies can move through stages without changing system ownership:

```text
local compose -> single pod candidate -> managed AWS candidate
```

This matters most for Kafka and stream processing.
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
