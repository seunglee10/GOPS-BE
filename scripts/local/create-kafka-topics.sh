#!/usr/bin/env bash
# 역할: 로컬 Docker Kafka에 GOPS canonical topic을 생성합니다.
# 사용: docker compose up 이후 한 번 실행하거나 kafka-init 서비스 대신 수동 실행합니다.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOPICS_FILE="${ROOT_DIR}/platform/kafka/topics.txt"

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

while IFS= read -r topic; do
  if [[ -z "${topic}" || "${topic}" == \#* ]]; then
    continue
  fi
  partitions=3
  if [[ "${topic}" == "market.input.realtime.trades.v1" || "${topic}" == "market.input.realtime.quotes.v1" ]]; then
    partitions=12
  fi
  create_topic "${topic}" "${partitions}"

  retention_ms=""
  segment_ms=""
  segment_bytes=""
  case "${topic}" in
    agents.market-events.v1)
      retention_ms="3600000"
      segment_ms="600000"
      segment_bytes="134217728"
      ;;
    market.input.realtime.trades.v1|market.input.realtime.quotes.v1|market.layer.trades.v1|market.layer.quotes.v1)
      retention_ms="7200000"
      segment_ms="900000"
      segment_bytes="268435456"
      ;;
  esac
  if [[ -n "${retention_ms}" ]]; then
    docker exec alfaka-kafka /opt/kafka/bin/kafka-configs.sh \
      --bootstrap-server localhost:9092 \
      --entity-type topics \
      --entity-name "${topic}" \
      --alter \
      --add-config "retention.ms=${retention_ms},segment.ms=${segment_ms},segment.bytes=${segment_bytes}"
  fi
done < "${TOPICS_FILE}"

docker exec alfaka-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
