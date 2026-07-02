# Kafka Platform Contract

Current local stage:

```text
docker-compose kafka
docker-compose kafka-init
platform/kafka/topics.txt
```

Staged path:

```text
local compose -> single Kafka pod candidate -> MSK candidate
```

Main env:

```text
KAFKA_BOOTSTRAP_SERVERS
KAFKA_PROCESSED_TOPICS=market.ticks.v1,market.candles.closed.v1,market.status.v1,market.volume-profile-bins.1m.v1
AGENT_EVENT_INPUT_TOPICS=market.ticks.v1,market.candles.closed.v1
KAFKA_PROCESSOR_ENABLE_AUTO_COMMIT=false
KAFKA_S3_ENABLE_AUTO_COMMIT=false
```

`platform/kafka/topics.txt` is the canonical market/order/agent topic list for local creation and future MSK creation.

v2 market-data removes the live candle Kafka path. `market.candles.live.1m.v1` can remain in an existing MSK cluster until operations confirms lag and consumer count are 0, then deletes it manually.
