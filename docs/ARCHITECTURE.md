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
    Frontend["gops-frontend<br/>chart panel and quote lists"]
    ChartEngine["chart-engine"]
  end

  subgraph Api["systems/api-server"]
    ApiServer["pod: api-server<br/>FastAPI REST and WebSocket"]
  end

  subgraph Agents["systems/agent-orchestration"]
    AgentOrch["pod: agent-orchestrator<br/>role agents"]
    EventDetector["pod: agent-event-detector"]
    AlertPublisher["pod: agent-notification-publisher"]
    AgentShared["shared: gops_agents.*"]
  end

  subgraph Market["systems/market-data"]
    Ingestor["pod: market-ingestor-sip<br/>default SIP WebSocket"]
    ExtraIngestors["optional: market-ingestor-iex/boats<br/>explicit only"]
    Processor["pod: market-processor"]
    CHLoader["pod: clickhouse-loader<br/>ClickHouse plus post-insert archive"]
    Backfill["pod: backfill-worker"]
    Registry["job: symbol-registry-sync"]
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
    Alpaca["Alpaca"]
    KIS["KIS demo API"]
  end

  User --> Frontend
  Frontend --> ChartEngine
  Frontend -->|"/api/charts, /ws/charts, /ws/quotes"| ApiServer

  ApiServer --> Redis
  ApiServer --> ClickHouse
  ApiServer --> Postgres
  ApiServer --> AgentOrch
  ApiServer --> MarketShared
  ApiServer --> OrderShared

  Alpaca --> Ingestor
  Alpaca -.-> ExtraIngestors
  Ingestor --> Kafka
  Ingestor --> Secrets
  Ingestor --> MarketShared
  ExtraIngestors -.-> Kafka
  ExtraIngestors -.-> Secrets
  ExtraIngestors -.-> MarketShared
  Kafka --> Processor
  Kafka --> EventDetector
  Processor --> Redis
  Processor --> Kafka
  Processor --> MarketShared
  EventDetector --> Kafka
  AgentOrch --> AgentShared
  Kafka --> AlertPublisher
  AlertPublisher --> Redis
  Kafka --> CHLoader
  CHLoader --> ClickHouse
  CHLoader -.->|post-insert archive| S3
  Backfill --> Redis
  Backfill --> ClickHouse
  Backfill --> Alpaca
  Backfill -.->|processed archive after ClickHouse| S3
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

## Market Data Serving Flow

```mermaid
flowchart LR
  Chart["Browser chart"]
  Quotes["Watch List and Hot Ranking"]
  RangePlanner["chart-engine range planner<br/>visible range plus session-aware buffer"]

  CandlesApi["GET /api/charts/candles<br/>latest or from/to range"]
  BackfillApi["POST /api/charts/backfill<br/>explicit start/end"]
  ChartWs["/ws/charts"]
  QuotesWs["/ws/quotes<br/>batched, maxHz"]
  WatchlistApi["PUT /api/charts/watchlist"]
  HotApi["GET /api/charts/hot-symbols"]

  RedisLatest["Redis latest/recent state"]
  TradeTiers["Redis active/watchlist/hot tiers"]
  BackfillQueue["Redis Streams<br/>backfill status/queue"]
  ClickHouse["ClickHouse chart_candles"]
  BackfillWorker["backfill-worker"]
  AlpacaHistorical["Alpaca historical bars"]
  AlpacaStream["Alpaca SIP live stream<br/>default single connection"]
  OptionalFeeds["Optional IEX/BOATS streams<br/>explicit only"]
  KafkaRaw["Kafka raw topics"]
  Processor["market-processor"]
  KafkaProcessed["Kafka processed topics"]
  CHLoader["clickhouse-loader"]
  S3Archive["S3 archive<br/>optional post-write evidence"]

  Chart --> CandlesApi
  Chart --> RangePlanner
  RangePlanner -->|from/to plus limit| CandlesApi
  RangePlanner --> BackfillApi
  CandlesApi --> RedisLatest
  CandlesApi --> ClickHouse
  RedisLatest --> CandlesApi
  ClickHouse --> CandlesApi

  BackfillApi --> BackfillQueue
  BackfillQueue --> BackfillWorker
  BackfillWorker -->|monthly partition chunks| ClickHouse
  BackfillWorker --> AlpacaHistorical
  AlpacaHistorical --> BackfillWorker
  BackfillWorker -.->|best effort after ClickHouse accept| S3Archive

  AlpacaStream --> KafkaRaw
  OptionalFeeds -.-> KafkaRaw
  KafkaRaw --> Processor
  Processor --> RedisLatest
  Processor --> KafkaProcessed
  KafkaProcessed --> CHLoader
  CHLoader --> ClickHouse
  CHLoader -.->|best effort after ClickHouse accept| S3Archive

  Chart --> ChartWs
  ChartWs --> RedisLatest
  Quotes --> QuotesWs
  QuotesWs --> RedisLatest
  Quotes --> WatchlistApi
  Quotes --> HotApi
  WatchlistApi --> TradeTiers
  HotApi --> ClickHouse
  HotApi --> RedisLatest
  HotApi --> TradeTiers
  ChartWs --> TradeTiers
  TradeTiers -.->|trade subscription set| AlpacaStream
```

The chart serving path reads Redis and ClickHouse. Initial chart snapshots can use a latest `limit`, but pan/zoom history loads use the chart-engine range planner to ask `/api/charts/candles` for the visible range plus buffer with half-open `from`/`to` bounds. Online backfill/gapfill checks ClickHouse coverage first, fetches only missing Alpaca source buckets, writes accepted candles to ClickHouse in `event_time` month-partition chunks, and may archive the same processed candles to S3 afterward. Historical Alpaca bars are split-adjusted by default, and the serving/backfill paths share `MARKET_DATA_MAX_HISTORY_YEARS` so the chart does not render or request rows older than the subscription window. Intraday frontend backfill windows are calculated in regular-session market minutes, so panning across an open, close, weekend, or overnight gap still asks for the prior tradable candles instead of an empty wall-clock range. S3 is not a chart-serving source and is not a prerequisite for chart rendering.

Trade ticks are realtime-only. Chart WebSocket sessions, user Watch List symbols, and Hot Ranking symbols are mirrored into Redis tier keys; the Alpaca ingestor resolves those tiers into the active trade subscription set. Active chart symbols have priority. Watch List and Hot Ranking symbols are capped before they reach Alpaca. `/ws/quotes` separately reads Redis live/closed candle state for UI updates at `maxHz=1` by default and does not mutate the Alpaca trade subscription tiers.

The default live Alpaca runtime opens one SIP WebSocket ingestor. IEX and BOATS ingestors are optional extra runtimes and must be enabled explicitly after confirming the Alpaca account can open additional feed connections.

## Realtime Trade Tier Contract

```mermaid
sequenceDiagram
  participant Chart as Browser chart
  participant Quotes as Watch List / Hot Ranking UI
  participant API as GOPS API
  participant Redis as Redis tier keys
  participant Ingestor as Alpaca ingestor
  participant Alpaca as Alpaca WebSocket
  participant Kafka as Kafka raw trades
  participant Processor as market-processor

  Chart->>API: Open /ws/charts?symbol=AAPL&interval=1m
  API->>Redis: Refresh active:charts:AAPL immediately, then keep TTL alive
  Quotes->>API: PUT /api/charts/watchlist
  API->>Redis: Replace watchlist:symbols
  Quotes->>API: GET /api/charts/hot-symbols
  API->>Redis: Persist hot:symbols and hot:symbols:snapshot
  loop every ALPACA_ACTIVE_POLL_SECONDS
    Ingestor->>Redis: Read active + watchlist + hot snapshot ranking
    Ingestor->>Ingestor: Resolve priority and caps
    Ingestor->>Alpaca: Send subscribe/unsubscribe trade diffs
  end
  Alpaca-->>Ingestor: trade ticks for subscribed symbols
  Ingestor->>Kafka: market.raw.trades
  Processor->>Redis: realtime price/live candle/profile updates
  Processor-->>Kafka: processed candle/status/profile topics
```

The active chart tier is refreshed before chart gap-fill is sent so a newly opened chart can enter the trade subscription set without waiting for historical repair or heartbeat traffic. The Hot Ranking tier is read from the ordered Redis snapshot first, then the unordered Redis set is used only as a fallback, so caps such as Top 10 preserve the current dollar-volume ranking. Trade ticks are not inserted into ClickHouse and are not archived to S3 by default.

## Backfill Contract

```mermaid
sequenceDiagram
  participant Chart as Browser chart
  participant API as Chart API
  participant Redis as Redis queue/status
  participant CH as ClickHouse chart_candles
  participant Worker as backfill-worker
  participant Alpaca as Alpaca historical bars
  participant S3 as S3 optional archive

  Chart->>API: GET /api/charts/candles?from/to&limit visible range plus buffer
  API->>CH: read requested range and coverage
  API-->>Chart: candles plus coverage/repair metadata
  alt range is missing or repairable
    Chart->>API: POST /api/charts/backfill start/end
    API->>Redis: store status and enqueue request
    Worker->>Redis: claim request
    Worker->>CH: scan timestamps in requested range
    Worker->>Alpaca: fetch only missing source buckets
    Alpaca-->>Worker: bars pages plus next_page_token
    Worker->>CH: materialize accepted candles by event_time month
    Worker-->>S3: best-effort archive after ClickHouse accept
    Worker->>Redis: mark succeeded/failed/unavailable
    Chart->>API: refetch same from/to range
    API->>CH: read newly materialized range
    API-->>Chart: renderable candles
  end
```

The contract has no S3-to-chart, S3-to-ClickHouse preload, or Kafka-to-S3 sidecar edge. S3 can preserve evidence or recovery artifacts after runtime writes, but only after ClickHouse accepts the corresponding rows. ClickHouse is the serving source of truth and Redis is the realtime/status/cache layer. Backfill materialization must keep ClickHouse inserts bounded to the `chart_candles` monthly partition key, so a long daily range does not put more than one month of candles in a single INSERT block. Derived chart intervals backfill their stored source interval: `5m` and `10m` fill `1m`; `1W` and `1M` fill `1D`. Weekly and monthly serving queries aggregate stored `1D` rows and filter by the higher-timeframe bucket timestamp, so a non-boundary `from`/`to` request does not create a partial first or last weekly/monthly candle. Normal backfill repairs only missing buckets; `force=true` refetches the full clamped source range and lets newer ClickHouse rows supersede stale rows.

## Platform Staging

Platform dependencies can move through stages without changing system ownership:

```text
local compose -> single pod candidate -> managed AWS candidate
```

This matters most for Kafka and Flink/stream processing.
Keep folder structure platform-neutral; platform-specific deployment details belong under `infra/` and `platform/`.

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
