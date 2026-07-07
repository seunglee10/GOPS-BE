# Kafka Platform Contract

Kafka is the ordered handoff layer for the on-demand chart data rebuild.
Market-data producers must use `key=symbol`; one partition is handled by one
consumer pod at a time.

## Canonical Topic File

```text
platform/kafka/topics.txt
infra/k8s/base/platform/kafka/topics.txt
```

## Input Topics

```text
market.input.realtime.trades.v1
market.input.realtime.quotes.v1
market.input.realtime.events.v1
market.input.realtime.bars.1m.v1
market.input.realtime.updated-bars.1m.v1
market.input.realtime.daily-bars.v1
```

`market.input.realtime.quotes.v1` is consumed by the quote processor, written
to Redis/WebSocket live state, and republished as `market.layer.quotes.v1` for
canonical S3/ClickHouse storage.

## Tick Fanout Topics

```text
market.realtime.ticks.to.1m.v1
market.realtime.ticks.to.5m.v1
market.realtime.ticks.to.10m.v1
market.realtime.ticks.to.1d.v1
market.realtime.ticks.to.1w.v1
market.realtime.ticks.to.1mo.v1
```

These topics are legacy/debug fanout streams. The default processor hot path
does not re-consume them; raw trades update 1m/live state directly.

## Layer Topics

```text
market.layer.candles.live.v1
market.layer.candles.closed.v1
market.layer.candles.1m.closed.v1
market.layer.candles.5m.closed.v1
market.layer.candles.10m.closed.v1
market.layer.candles.1h.closed.v1
market.layer.candles.4h.closed.v1
market.layer.candles.1d.closed.v1
market.layer.candles.1w.closed.v1
market.layer.candles.1mo.closed.v1
market.layer.trades.v1
market.layer.quotes.v1
market.layer.events.v1
```

Closed candle layer topics are interval-specific so storage and future
interval-specific processors can be scaled independently. The payload still
carries the canonical `interval` field. `market.layer.candles.closed.v1`
remains listed as a legacy compatibility topic; new processor config publishes
closed candles to the interval-specific topics.

`platform/kafka/topics.txt` is the canonical market/order/agent topic list for
local creation and future MSK creation. Agent analysis uses separate hot and
deep request topics so deep backlog does not consume the hot worker group.

Agent topics:

```text
agents.analysis-requests.v1
agents.deep-analysis-requests.v1
agents.analysis-results.v1
agents.query-understanding-events.v1
agents.notification-decisions.v1
agents.dlq.v1
```

`agents.query-understanding-events.v1` is an observability/audit stream emitted
with completed reports. It is not used as request/reply transport inside the
hot query-understanding fan-out.

Any market-data topic not listed in the source-of-truth chart rebuild plan is
outside the current chart-data contract.
