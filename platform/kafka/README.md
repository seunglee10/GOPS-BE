# Kafka Platform Contract

Kafka is the ordered handoff layer for live chart facts and downstream projections.
Market-data producers must use `key=symbol`; one partition is handled by one
consumer pod at a time.

## Canonical Topic File

```text
platform/kafka/topics.txt
infra/k8s/base/platform/kafka/topics.txt
```

The platform copy is canonical for local scripts and Docker Compose. K8s keeps
a deployment-local mirror because `configMapGenerator` cannot read outside its
kustomization root; the contract checker requires equivalent topic values.

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
ClickHouse tick storage. The raw S3 sink intentionally excludes high-volume
trade and quote topics; processed S3 also does not consume the quote layer topic.

`market.input.realtime.trades.v1` and `market.input.realtime.quotes.v1` are the
hot raw streams. Creation helpers default them to 12 partitions so trade/candle
and quote processors can scale independently. Existing Kafka topics do not
change partition count just because the helper uses `--if-not-exists`; operators
must alter existing topics explicitly during a live cluster migration.

Raw/layer trade and quote topics use 2-hour retention with bounded segments;
`agents.market-events.v1` uses 1 hour. Local, Compose, and K8s creation paths
apply the same policy.

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

Live provisional candles are stored in Redis and delivered through
`market.events` pub/sub/WebSocket. They are not published to a Kafka layer
topic. Optional indicator and candle-volume-profile requests execute inside the
API and likewise do not use Kafka request/DLQ topics.

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
agents.chart-asset-build-requests.v1
agents.deep-analysis-requests.v1
agents.analysis-results.v1
agents.query-understanding-events.v1
agents.notification-decisions.v1
agents.dlq.v1
```

`agents.chart-asset-build-requests.v1` carries one manual chart-analysis asset
job per message. The independent `gops-chart-asset-builder` consumer group uses
`max_poll_records=1`, commits only after the job finishes, and does not share
the interactive analysis request/result path.
Asset v2 keeps this topic and key unchanged. One job message is processed as
symbol bundles: one canonical daily query and at most one multi-timeframe LLM
curation call per symbol. No candidate or interval subtopic is created.

`agents.query-understanding-events.v1` is an observability/audit stream emitted
with completed reports. It is not used as request/reply transport inside the
hot query-understanding fan-out.

Current chart-data ownership and compatibility rules are in
`docs/CHART_DATA_ARCHITECTURE.md`.
