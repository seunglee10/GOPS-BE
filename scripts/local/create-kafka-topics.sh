#!/usr/bin/env bash
# 역할: 로컬 Docker Kafka에 Raw/Processed topic을 생성합니다.
# 사용: docker compose up 이후 한 번 실행하거나 kafka-init 서비스 대신 수동 실행합니다.
# 출력: 생성된 Kafka topic 목록을 보여줍니다.
set -euo pipefail

for topic in \
  market.raw.bars \
  market.raw.updated-bars \
  market.raw.trades \
  market.raw.daily-bars \
  market.raw.statuses \
  market.raw.quotes \
  market.raw.corrections \
  market.raw.cancel-errors \
  market.ticks.v1 \
  market.candles.live.1m.v1 \
  market.candles.closed.v1 \
  market.status.v1 \
  market.volume-profile-bins.1m.v1 \
  orders.commands.v1 \
  broker.submit-results.v1 \
  broker.order-events.v1 \
  orders.dlq.v1 \
  agents.market-events.v1 \
  agents.analysis-requests.v1 \
  agents.analysis-results.v1 \
  agents.notification-decisions.v1 \
  agents.dlq.v1; do
  docker exec alfaka-kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 \
    --create \
    --if-not-exists \
    --topic "${topic}" \
    --partitions 3 \
    --replication-factor 1
done

docker exec alfaka-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
