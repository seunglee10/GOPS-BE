#!/usr/bin/env bash
# 역할: 로컬 Docker Kafka에 GOPS canonical topic을 생성합니다.
# 사용: docker compose up 이후 한 번 실행하거나 kafka-init 서비스 대신 수동 실행합니다.
set -euo pipefail

hot_topics=(
  market.input.realtime.trades.v1
  market.input.realtime.quotes.v1
)

standard_topics=(
  market.input.realtime.events.v1
  market.input.realtime.bars.1m.v1
  market.input.realtime.updated-bars.1m.v1
  market.input.realtime.daily-bars.v1
  market.realtime.ticks.to.1m.v1
  market.realtime.ticks.to.5m.v1
  market.realtime.ticks.to.10m.v1
  market.realtime.ticks.to.1d.v1
  market.realtime.ticks.to.1w.v1
  market.realtime.ticks.to.1mo.v1
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
  market.news.alpaca.v1
  market.news.daily-summary-dirty.v1
  orders.commands.v1
  broker.submit-results.v1
  broker.order-events.v1
  orders.dlq.v1
  alerts.triggered.v1
  alerts.dlq.v1
  agents.market-events.v1
  agents.analysis-requests.v1
  agents.deep-analysis-requests.v1
  agents.analysis-results.v1
  agents.query-understanding-events.v1
  agents.notification-decisions.v1
  agents.dlq.v1
)

create_topic() {
  local topic="$1"
  local partitions="$2"
  docker exec alfaka-kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 \
    --create \
    --if-not-exists \
    --topic "${topic}" \
    --partitions "${partitions}" \
    --replication-factor 1
}

for topic in "${hot_topics[@]}"; do
  create_topic "${topic}" 12
done

for topic in "${standard_topics[@]}"; do
  create_topic "${topic}" 3
done

docker exec alfaka-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
