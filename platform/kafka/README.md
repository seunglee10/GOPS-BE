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

`platform/kafka/topics.txt` is the canonical market/order/agent topic list for local creation and future MSK creation.

## Planned Chart Rebuild Topics

The upcoming chart-data rebuild in `../../docs/CHART_DATA_REBUILD_PLAN.md`
introduces more explicit market layer topics while keeping `key=symbol` as the
ordering rule:

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
```

Do not add these to `topics.txt` until the implementation change updates the
processors, storage consumers, compose/k8s topic creation, and environment docs
in the same change.
