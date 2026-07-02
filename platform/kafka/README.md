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
```

`platform/kafka/topics.txt` is the canonical market/order/agent topic list for local creation and future MSK creation. Agent analysis uses separate hot and deep request topics so deep backlog does not consume the hot worker group.
