# GOPS Kafka Architecture

This document describes the current repository contract and the dev EKS Kafka
topology observed on 2026-07-10. The repository defines 39 application topics
plus the internal `__consumer_offsets` topic. Because topic initialization is
create-only, operators must compare the broker inventory after deployment;
older broker topics are not an active application contract.

## Physical Topology

```mermaid
flowchart LR
  subgraph Clients["Kafka clients in namespace alfaka-market-data"]
    Producers["Producers<br/>Alpaca ingestors<br/>Backend API<br/>Agent workers<br/>Order outbox"]
    Consumers["Consumers<br/>Market processors<br/>Storage loaders<br/>Agent/order/news workers"]
    TopicInit["kafka-topic-init Job<br/>reads infra topic list<br/>creates missing topics"]
  end

  subgraph Services["Kubernetes services"]
    ClusterIP["kafka ClusterIP<br/>kafka:29092"]
    Headless["kafka-headless<br/>stable StatefulSet DNS<br/>29092 and 9093"]
  end

  subgraph StreamingNode["Karpenter NodePool: streaming"]
    subgraph KafkaPod["StatefulSet kafka-0"]
      Broker["Kafka broker<br/>node.id=1<br/>INTERNAL listener :29092"]
      Controller["KRaft controller<br/>controller listener :9093<br/>quorum voter 1"]
      Broker ---|"same JVM and pod"| Controller
    end
    PVC[("kafka-data PVC<br/>eks-auto-ebs<br/>30Gi RWO<br/>/var/lib/kafka/data")]
    KafkaPod --- PVC
  end

  Producers --> ClusterIP --> Broker
  Consumers --> ClusterIP
  TopicInit --> ClusterIP
  Headless --> Broker
  Controller --> Headless
```

| Setting | Current value |
| --- | --- |
| Kafka image | `apache/kafka:3.9.0` |
| Metadata mode | KRaft, no ZooKeeper |
| Broker count | 1 |
| Controller count | 1, same process as broker |
| Replication factor | 1 for every application and internal topic |
| Minimum in-sync replicas | 1 |
| Network | namespace-internal `PLAINTEXT` |
| Auto topic creation | enabled |
| Default partitions | 3 |
| Broker default retention | 168 hours, 7 days |
| Broker default segment size | 1 GiB |
| Producer acknowledgements | `acks=1` |
| JVM heap | `-Xms1g -Xmx2g` |
| Pod resources | request `1 CPU / 3Gi`, limit `2 CPU / 4Gi` |
| Persistent storage | EBS PVC `30Gi` mounted at `/var/lib/kafka/data` |

Because broker, controller, every leader partition, and every replica are on
`kafka-0`, the PVC protects broker logs across pod replacement but the cluster
has no broker-level high availability. When `kafka-0` is unavailable, every
producer and consumer waits for the same broker to recover.

## Partition, Ordering, And Offset Model

```mermaid
flowchart LR
  Message["Market event<br/>key=NVDA<br/>JSON value"]
  Partitioner["Producer partitioner<br/>hash serialized key<br/>mod partition count"]

  subgraph Topic["Example: market.input.realtime.trades.v1"]
    P0["P0<br/>offset 0..N"]
    P1["P1<br/>offset 0..N"]
    P2["P2<br/>offset 0..N"]
    P3["P3"]
    P4["P4"]
    P5["P5"]
    P6["P6"]
    P7["P7"]
    P8["P8"]
    P9["P9"]
    P10["P10"]
    P11["P11"]
  end

  subgraph ProcessorGroup["consumer group: alfaka-market-processor"]
    MP1["processor pod 1<br/>about 4 trade partitions"]
    MP2["processor pod 2<br/>about 4 trade partitions"]
    MP3["processor pod 3<br/>about 4 trade partitions"]
  end

  subgraph ArchiveGroup["consumer group: alfaka-raw-s3-archive"]
    Archive["raw archive pod<br/>all 12 trade partitions"]
  end

  GroupOffsets["__consumer_offsets<br/>50 compacted partitions<br/>offset stored per group/topic/partition"]

  Message --> Partitioner
  Partitioner --> P0
  Partitioner --> P1
  Partitioner --> P2
  Partitioner --> P3
  Partitioner --> P4
  Partitioner --> P5
  Partitioner --> P6
  Partitioner --> P7
  Partitioner --> P8
  Partitioner --> P9
  Partitioner --> P10
  Partitioner --> P11

  P0 --> MP1
  P1 --> MP1
  P2 --> MP1
  P3 --> MP1
  P4 --> MP2
  P5 --> MP2
  P6 --> MP2
  P7 --> MP2
  P8 --> MP3
  P9 --> MP3
  P10 --> MP3
  P11 --> MP3
  P0 --> Archive
  P1 --> Archive
  P2 --> Archive
  P3 --> Archive
  P4 --> Archive
  P5 --> Archive
  P6 --> Archive
  P7 --> Archive
  P8 --> Archive
  P9 --> Archive
  P10 --> Archive
  P11 --> Archive
  ProcessorGroup -. "independent committed offsets" .-> GroupOffsets
  ArchiveGroup -. "independent committed offsets" .-> GroupOffsets
```

- A partition is the unit of ordering and parallel assignment. Kafka guarantees
  order inside one partition, not across an entire topic.
- Every market-data producer uses `key=symbol`. The same serialized symbol key
  is therefore routed to the same partition while the partition count remains
  unchanged.
- A partition is assigned to at most one active consumer inside a consumer
  group. Different groups independently read the same partition.
- The 12-partition trade and quote input topics allow up to 12 active consumers
  per group. The current three processor replicas normally receive about four
  partitions each.
- Standard three-partition topics can use at most three active consumers in one
  group. Extra replicas would remain idle for that topic.

## Market Data Pipeline

```mermaid
flowchart LR
  subgraph Ingestors["Producers"]
    SIP["alpaca-ingestor-sip"]
    BOATS["alpaca-ingestor-boats"]
    Crypto["alpaca-ingestor-crypto"]
  end

  subgraph Raw["Raw input topics"]
    RT["market.input.realtime.trades.v1<br/>12P · RF1 · 2h"]
    RQ["market.input.realtime.quotes.v1<br/>12P · RF1 · 2h"]
    RE["market.input.realtime.events.v1<br/>3P · RF1 · 7d"]
    RB["market.input.realtime.bars.1m.v1<br/>3P · RF1 · 7d"]
    RU["market.input.realtime.updated-bars.1m.v1<br/>3P · RF1 · 7d"]
    RD["market.input.realtime.daily-bars.v1<br/>3P · RF1 · 7d"]
  end

  MarketProcessor["alfaka-market-processor<br/>group · 3 pods<br/>manual offset commit"]
  QuoteProcessor["alfaka-market-quote-processor<br/>group · 3 pods<br/>manual offset commit"]
  RawArchive["alfaka-raw-s3-archive<br/>group · 1 pod<br/>manual commit after S3 upload"]
  Redis[("Redis live state")]
  RawS3[("S3 raw archive")]

  subgraph Layer["Canonical layer topics"]
    LT["market.layer.trades.v1<br/>3P · RF1 · 2h"]
    LQ["market.layer.quotes.v1<br/>3P · RF1 · 2h"]
    LE["market.layer.events.v1<br/>3P · RF1 · 7d"]
    C1M["market.layer.candles.1m.closed.v1<br/>3P · RF1 · 7d"]
    C5M["market.layer.candles.5m.closed.v1<br/>3P · RF1 · 7d"]
    C10M["market.layer.candles.10m.closed.v1<br/>3P · RF1 · 7d"]
    C1H["market.layer.candles.1h.closed.v1<br/>3P · RF1 · 7d"]
    C4H["market.layer.candles.4h.closed.v1<br/>3P · RF1 · 7d"]
    C1D["market.layer.candles.1d.closed.v1<br/>3P · RF1 · 7d"]
    C1W["market.layer.candles.1w.closed.v1<br/>3P · RF1 · 7d"]
    C1MO["market.layer.candles.1mo.closed.v1<br/>3P · RF1 · 7d"]
  end

  TickLoader["ClickHouse tick loader<br/>same group · 3 pods"]
  CandleLoader["ClickHouse candle loader<br/>same group · 1 pod"]
  ProcessedS3["processed S3 sink<br/>group · 1 pod"]
  EventDetector["agent event detector<br/>group · 1 pod"]
  AlertEvaluator["alert evaluator<br/>group · 1 pod"]
  ClickHouse[("ClickHouse")]
  FinalS3[("S3 final and manifest")]

  SIP --> RT
  SIP --> RQ
  SIP --> RE
  SIP --> RB
  SIP --> RU
  SIP --> RD
  BOATS --> RT
  BOATS --> RQ
  BOATS --> RE
  BOATS --> RB
  BOATS --> RU
  BOATS --> RD
  Crypto --> RT
  Crypto --> RQ
  Crypto --> RB
  Crypto --> RU
  Crypto --> RD

  RT --> MarketProcessor
  RE --> MarketProcessor
  RB --> MarketProcessor
  RU --> MarketProcessor
  RD --> MarketProcessor
  RQ --> QuoteProcessor
  RT --> RawArchive
  RQ --> RawArchive
  RE --> RawArchive
  RB --> RawArchive
  RU --> RawArchive
  RD --> RawArchive
  RawArchive --> RawS3

  MarketProcessor --> LT
  MarketProcessor --> LE
  MarketProcessor --> C1M
  MarketProcessor --> C5M
  MarketProcessor --> C10M
  MarketProcessor --> C1H
  MarketProcessor --> C4H
  MarketProcessor --> C1D
  MarketProcessor --> C1W
  MarketProcessor --> C1MO
  MarketProcessor --> Redis
  QuoteProcessor --> LQ
  QuoteProcessor --> Redis

  LT --> TickLoader
  LQ --> TickLoader
  TickLoader --> ClickHouse
  C1M --> CandleLoader
  C5M --> CandleLoader
  C10M --> CandleLoader
  C1H --> CandleLoader
  C4H --> CandleLoader
  C1D --> CandleLoader
  C1W --> CandleLoader
  C1MO --> CandleLoader
  LE --> CandleLoader
  CandleLoader --> ClickHouse
  C1M --> ProcessedS3
  C5M --> ProcessedS3
  C10M --> ProcessedS3
  C1H --> ProcessedS3
  C4H --> ProcessedS3
  C1D --> ProcessedS3
  C1W --> ProcessedS3
  C1MO --> ProcessedS3
  LE --> ProcessedS3
  ProcessedS3 --> FinalS3
  LT --> EventDetector
  C1M --> EventDetector
  C5M --> EventDetector
  C10M --> EventDetector
  C1H --> EventDetector
  C4H --> EventDetector
  C1D --> EventDetector
  C1W --> EventDetector
  C1MO --> EventDetector
  LE --> EventDetector
  LT --> AlertEvaluator
```

The following market topics exist but are not in the active hot path:

```mermaid
flowchart LR
  Processor["market processor"]
  Disabled{"KAFKA_PUBLISH_TICK_FANOUT=false"}
  F1["market.realtime.ticks.to.1m.v1"]
  F5["market.realtime.ticks.to.5m.v1"]
  F10["market.realtime.ticks.to.10m.v1"]
  FD["market.realtime.ticks.to.1d.v1"]
  FW["market.realtime.ticks.to.1w.v1"]
  FM["market.realtime.ticks.to.1mo.v1"]
  Legacy["market.layer.candles.closed.v1<br/>legacy compatibility topic"]

  Processor -. "disabled" .-> Disabled
  Disabled -.-> F1
  Disabled -.-> F5
  Disabled -.-> F10
  Disabled -.-> FD
  Disabled -.-> FW
  Disabled -.-> FM
  Processor -. "interval-specific topics used instead" .-> Legacy
```

Redis and the `market.events` pub/sub channel are the active live chart state;
there is no live-candle Kafka topic. The processed S3 sink stores only interval
closed candles and events. Trades and quotes are archived through raw S3 and
loaded into ClickHouse by the tick loader.

## News, API Derived Data, And Alerts

```mermaid
flowchart LR
  NewsIngestor["Alpaca news ingestor"]
  NewsTopic["market.news.alpaca.v1<br/>3P · RF1 · 7d"]
  NewsLoader["ClickHouse candle/news loader<br/>group alfaka-clickhouse-loader"]
  NewsIntel["news intelligence worker<br/>group · 3 pods · manual commit"]
  Dirty["market.news.daily-summary-dirty.v1<br/>3P · RF1 · 7d"]
  Daily["daily summary worker<br/>group · 2 pods · manual commit"]

  Canonical["Canonical candle facade"]
  Backend["Backend indicators and volume profile<br/>inline request compute"]

  Trades["market.layer.trades.v1"]
  Alert["alert evaluator<br/>group · 1 pod · manual commit"]
  RedisOutbox["Redis Stream alerts:outbox"]
  Triggered["alerts.triggered.v1<br/>audit/replay · no consumer"]
  AlertDLQ["alerts.dlq.v1<br/>no consumer"]

  ClickHouse[("ClickHouse")]
  Redis[("Redis cache and WebSocket pubsub")]
  Postgres[("Postgres notifications")]

  NewsIngestor --> NewsTopic
  NewsTopic --> NewsLoader --> ClickHouse
  NewsTopic --> NewsIntel
  NewsIntel --> ClickHouse
  NewsIntel --> Redis
  NewsIntel --> Dirty --> Daily
  Daily --> ClickHouse
  Daily --> Redis

  ClickHouse --> Canonical
  Redis --> Canonical
  Canonical --> Backend --> Redis

  Trades --> Alert --> RedisOutbox
  RedisOutbox --> Postgres
  RedisOutbox --> Redis
  RedisOutbox --> Triggered
  Alert -->|"processing failure"| AlertDLQ
```

## Agent Topics

```mermaid
flowchart LR
  MarketLayer["market layer topics"]
  Detector["agent-event-detector<br/>group gops-agent-event-detector"]
  MarketEvents["agents.market-events.v1<br/>3P · RF1 · 1h<br/>no current consumer"]

  Backend["Backend API"]
  Requests["agents.analysis-requests.v1<br/>3P · RF1 · 7d"]
  HotWorker["agent-analysis-worker<br/>group · 1 pod · manual commit"]
  DeepRequests["agents.deep-analysis-requests.v1<br/>3P · RF1 · 7d"]
  DeepWorker["deep-analysis-worker<br/>group · 1 pod · manual commit"]

  Results["agents.analysis-results.v1<br/>3P · RF1 · 7d"]
  QueryAudit["agents.query-understanding-events.v1<br/>audit · no consumer"]
  Decisions["agents.notification-decisions.v1<br/>3P · RF1 · 7d"]
  AgentDLQ["agents.dlq.v1<br/>reserved · no active producer/consumer"]

  Delivery["agent-delivery-gateway<br/>group · 1 pod · manual commit"]
  Notification["agent-notification-publisher<br/>group · 1 pod · auto commit default"]
  Redis[("Redis report store<br/>SSE and alert pubsub")]

  MarketLayer --> Detector --> MarketEvents
  Backend --> Requests --> HotWorker
  HotWorker -->|"only when deep analysis is enabled"| DeepRequests --> DeepWorker
  HotWorker --> Results
  DeepWorker --> Results
  HotWorker --> QueryAudit
  DeepWorker --> QueryAudit
  HotWorker --> Decisions
  DeepWorker --> Decisions
  Results --> Delivery --> Redis
  Decisions --> Notification --> Redis
  HotWorker -. "reserved topic, not active failure path" .-> AgentDLQ
```

The hot and deep workers run the `AgentOrchestrator` in-process. The standalone
`agent-orchestrator` HTTP service can also publish results and notification
decisions for compatibility requests. Invalid or failed async analysis is
currently represented by a failed report on `agents.analysis-results.v1`; the
`agents.dlq.v1` topic exists but is not the active failure path.

## Order Topics

```mermaid
flowchart LR
  Backend["POST /api/orders"]
  Postgres[("Postgres<br/>orders · events · outbox_events · dlq_events")]
  Outbox["order-outbox-publisher<br/>1 pod"]
  Commands["orders.commands.v1<br/>3P · RF1 · 7d"]
  Adapter["kis-broker-adapter<br/>group · 1 pod<br/>manual synchronous commit"]
  KIS["KIS demo API"]
  Submit["broker.submit-results.v1<br/>3P · RF1 · 7d<br/>no current Kafka consumer"]
  Reconciler["order reconciler job"]
  Events["broker.order-events.v1<br/>3P · RF1 · 7d<br/>no current Kafka consumer"]
  DLQ["orders.dlq.v1<br/>3P · RF1 · 7d<br/>no current Kafka producer/consumer"]

  Backend -->|"single DB transaction"| Postgres
  Postgres -->|"orders.commands outbox row"| Outbox --> Commands
  Commands --> Adapter --> KIS
  Adapter -->|"submission result and outbox row"| Postgres
  Postgres -->|"submit result outbox row"| Outbox --> Submit
  Reconciler -->|"broker event and outbox row"| Postgres
  Postgres -->|"order event outbox row"| Outbox --> Events
  Adapter -->|"invalid command stored in Postgres dlq_events"| Postgres
  Adapter -. "does not currently publish here" .-> DLQ
```

## Complete Topic Inventory

Every application topic has replication factor 1. `7d` below means no topic
override, so the broker default `log.retention.hours=168` applies.

| # | Topic | Partitions | Retention | Producer | Current consumer group |
| ---: | --- | ---: | ---: | --- | --- |
| 1 | `market.input.realtime.trades.v1` | 12 | 2h | Alpaca ingestors | `alfaka-market-processor`, `alfaka-raw-s3-archive` |
| 2 | `market.input.realtime.quotes.v1` | 12 | 2h | Alpaca ingestors | `alfaka-market-quote-processor`, `alfaka-raw-s3-archive` |
| 3 | `market.input.realtime.events.v1` | 3 | 7d | Alpaca ingestors | `alfaka-market-processor`, `alfaka-raw-s3-archive` |
| 4 | `market.input.realtime.bars.1m.v1` | 3 | 7d | Alpaca ingestors | `alfaka-market-processor`, `alfaka-raw-s3-archive` |
| 5 | `market.input.realtime.updated-bars.1m.v1` | 3 | 7d | Alpaca ingestors | `alfaka-market-processor`, `alfaka-raw-s3-archive` |
| 6 | `market.input.realtime.daily-bars.v1` | 3 | 7d | Alpaca ingestors | `alfaka-market-processor`, `alfaka-raw-s3-archive` |
| 7 | `market.realtime.ticks.to.1m.v1` | 3 | 7d | disabled processor fanout | none |
| 8 | `market.realtime.ticks.to.5m.v1` | 3 | 7d | disabled processor fanout | none |
| 9 | `market.realtime.ticks.to.10m.v1` | 3 | 7d | disabled processor fanout | none |
| 10 | `market.realtime.ticks.to.1d.v1` | 3 | 7d | disabled processor fanout | none |
| 11 | `market.realtime.ticks.to.1w.v1` | 3 | 7d | disabled processor fanout | none |
| 12 | `market.realtime.ticks.to.1mo.v1` | 3 | 7d | disabled processor fanout | none |
| 13 | `market.layer.candles.closed.v1` | 3 | 7d | legacy compatibility | none |
| 14 | `market.layer.candles.1m.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 15 | `market.layer.candles.5m.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 16 | `market.layer.candles.10m.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 17 | `market.layer.candles.1h.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 18 | `market.layer.candles.4h.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 19 | `market.layer.candles.1d.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 20 | `market.layer.candles.1w.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 21 | `market.layer.candles.1mo.closed.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 22 | `market.layer.trades.v1` | 3 | 2h | market processor | ClickHouse tick loader, event detector, alert evaluator |
| 23 | `market.layer.quotes.v1` | 3 | 2h | quote processor | ClickHouse tick loader |
| 24 | `market.layer.events.v1` | 3 | 7d | market processor | ClickHouse, processed S3, event detector |
| 25 | `market.news.alpaca.v1` | 3 | 7d | news ingestor and recent backfill | ClickHouse loader, news intelligence |
| 26 | `market.news.daily-summary-dirty.v1` | 3 | 7d | news intelligence | news daily summary worker |
| 27 | `orders.commands.v1` | 3 | 7d | order outbox publisher | `kis-broker-adapter` |
| 28 | `broker.submit-results.v1` | 3 | 7d | order outbox publisher | none |
| 29 | `broker.order-events.v1` | 3 | 7d | order outbox publisher | none |
| 30 | `orders.dlq.v1` | 3 | 7d | reserved; active DLQ is Postgres | none |
| 31 | `alerts.triggered.v1` | 3 | 7d | alert evaluator outbox sender | none; audit/replay topic |
| 32 | `alerts.dlq.v1` | 3 | 7d | alert evaluator | none |
| 33 | `agents.market-events.v1` | 3 | 1h | agent event detector | none |
| 34 | `agents.analysis-requests.v1` | 3 | 7d | Backend API | `gops-agent-analysis-worker` |
| 35 | `agents.deep-analysis-requests.v1` | 3 | 7d | analysis worker when enabled | `gops-agent-deep-analysis-worker` |
| 36 | `agents.analysis-results.v1` | 3 | 7d | analysis/deep worker or orchestrator | `gops-agent-delivery-gateway` |
| 37 | `agents.query-understanding-events.v1` | 3 | 7d | analysis/deep worker or orchestrator | none; audit topic |
| 38 | `agents.notification-decisions.v1` | 3 | 7d | analysis/deep worker or orchestrator | `gops-agent-notification-publisher` |
| 39 | `agents.dlq.v1` | 3 | 7d | reserved | none |

Internal topic:

| Topic | Partitions | Replication | Cleanup | Purpose |
| --- | ---: | ---: | --- | --- |
| `__consumer_offsets` | 50 | 1 | compact | committed offset per consumer group, topic, and partition |

## Consumer Groups And Commit Policy

| Consumer group | Pods | Topics | Commit policy |
| --- | ---: | --- | --- |
| `alfaka-market-processor` | 3 | trade, bar, updated bar, daily bar, event raw topics | manual |
| `alfaka-market-quote-processor` | 3 | raw quote | manual |
| `alfaka-raw-s3-archive` | 1 | all six raw topics | manual after every S3 side effect succeeds |
| `alfaka-processed-s3-sink` | 1 | eight closed candle topics and layer events | manual after S3 buffer flush |
| `alfaka-clickhouse-loader` | 4 members | one candle/news loader plus three trade/quote loaders | manual after ClickHouse insert |
| `alfaka-news-intelligence-worker` | 3 | Alpaca news | manual after Redis/ClickHouse side effects |
| `alfaka-news-daily-summary-worker` | 2 | daily summary dirty | manual after Redis/ClickHouse side effects |
| `gops-agent-event-detector` | 1 | trades, eight closed candle topics, events | auto commit default |
| `gops-alert-evaluator` | 1 | layer trades | manual after trade evaluation |
| `gops-agent-analysis-worker` | 1 | analysis requests | manual after report processing |
| `gops-agent-deep-analysis-worker` | 1 | deep analysis requests | manual after report processing |
| `gops-agent-delivery-gateway` | 1 | analysis results | manual after Redis delivery |
| `gops-agent-notification-publisher` | 1 | notification decisions | auto commit default |
| `kis-broker-adapter` | 1 | order commands | manual synchronous commit after adapter processing |

The raw and processed S3 sinks explicitly disable auto commit. They flush every
buffered S3 side effect before committing the consumer position; a failed upload
leaves the offset uncommitted for replay. ClickHouse loaders use the same
side-effect-before-commit policy.

## Retention Overrides

| Topic | Retention | Segment time | Segment size |
| --- | ---: | ---: | ---: |
| `agents.market-events.v1` | 1 hour | 10 minutes | 128 MiB |
| `market.input.realtime.trades.v1` | 2 hours | 15 minutes | 256 MiB |
| `market.input.realtime.quotes.v1` | 2 hours | 15 minutes | 256 MiB |
| `market.layer.trades.v1` | 2 hours | 15 minutes | 256 MiB |
| `market.layer.quotes.v1` | 2 hours | 15 minutes | 256 MiB |
| every other application topic | 7 days | broker default | broker default 1 GiB |

Kafka deletes data by closed log segment. The short-retention topics therefore
set both `segment.ms` and `segment.bytes`; setting only `retention.ms` would let
an active default segment survive beyond the intended retention window.

## Repository And Broker Reconciliation

`platform/kafka/topics.txt` is the canonical topic list. The K8s copy is checked
for byte-for-byte equality, and local topic creation reads the canonical file.
The topic-init Job creates missing topics but intentionally does not delete
broker state. After deploying a contract change, an operator compares the
broker inventory and consumer groups with this document before separately
retiring unreferenced topics.

## Inspection Commands

```bash
kubectl exec -n alfaka-market-data kafka-0 -- \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:29092 --describe

kubectl exec -n alfaka-market-data kafka-0 -- \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:29092 --list

kubectl exec -n alfaka-market-data kafka-0 -- \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:29092 \
  --describe --all-groups
```
