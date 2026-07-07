#!/usr/bin/env bash
# 역할: AWS MSK 또는 Kafka cluster에 필요한 topic을 생성합니다.
# 사용: KAFKA_BOOTSTRAP_SERVERS와 kafka-topics.sh가 있는 환경에서 실행합니다.
# 주의: MSK IAM 인증을 쓰면 client.properties를 추가로 넘겨야 합니다.
set -euo pipefail

KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:?KAFKA_BOOTSTRAP_SERVERS를 넣어주세요}"
KAFKA_TOPICS_FILE="${KAFKA_TOPICS_FILE:-platform/kafka/topics.txt}"
PARTITIONS="${PARTITIONS:-6}"
HOT_TOPIC_PARTITIONS="${HOT_TOPIC_PARTITIONS:-12}"
REPLICATION_FACTOR="${REPLICATION_FACTOR:-3}"
CLIENT_CONFIG_ARG=()

if [ -n "${KAFKA_CLIENT_CONFIG:-}" ]; then
  CLIENT_CONFIG_ARG=(--command-config "${KAFKA_CLIENT_CONFIG}")
fi

while IFS= read -r topic; do
  if [[ -z "${topic}" || "${topic}" == \#* ]]; then
    continue
  fi
  topic_partitions="${PARTITIONS}"
  case "${topic}" in
    market.input.realtime.trades.v1|market.input.realtime.quotes.v1)
      topic_partitions="${HOT_TOPIC_PARTITIONS}"
      ;;
  esac

  kafka-topics.sh \
    --bootstrap-server "${KAFKA_BOOTSTRAP_SERVERS}" \
    "${CLIENT_CONFIG_ARG[@]}" \
    --create \
    --if-not-exists \
    --topic "${topic}" \
    --partitions "${topic_partitions}" \
    --replication-factor "${REPLICATION_FACTOR}"
done < "${KAFKA_TOPICS_FILE}"

kafka-topics.sh \
  --bootstrap-server "${KAFKA_BOOTSTRAP_SERVERS}" \
  "${CLIENT_CONFIG_ARG[@]}" \
  --list
