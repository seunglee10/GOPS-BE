#!/usr/bin/env bash
# 역할: AWS MSK 또는 Kafka cluster에 필요한 topic을 생성합니다.
# 사용: KAFKA_BOOTSTRAP_SERVERS와 kafka-topics.sh가 있는 환경에서 실행합니다.
# 주의: MSK IAM 인증을 쓰면 client.properties를 추가로 넘겨야 합니다.
set -euo pipefail

KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:?KAFKA_BOOTSTRAP_SERVERS를 넣어주세요}"
KAFKA_TOPICS_FILE="${KAFKA_TOPICS_FILE:-infra/aws/msk/topics.txt}"
PARTITIONS="${PARTITIONS:-6}"
REPLICATION_FACTOR="${REPLICATION_FACTOR:-3}"
CLIENT_CONFIG_ARG=()

if [ -n "${KAFKA_CLIENT_CONFIG:-}" ]; then
  CLIENT_CONFIG_ARG=(--command-config "${KAFKA_CLIENT_CONFIG}")
fi

while IFS= read -r topic; do
  if [[ -z "${topic}" || "${topic}" == \#* ]]; then
    continue
  fi

  kafka-topics.sh \
    --bootstrap-server "${KAFKA_BOOTSTRAP_SERVERS}" \
    "${CLIENT_CONFIG_ARG[@]}" \
    --create \
    --if-not-exists \
    --topic "${topic}" \
    --partitions "${PARTITIONS}" \
    --replication-factor "${REPLICATION_FACTOR}"
done < "${KAFKA_TOPICS_FILE}"

kafka-topics.sh \
  --bootstrap-server "${KAFKA_BOOTSTRAP_SERVERS}" \
  "${CLIENT_CONFIG_ARG[@]}" \
  --list
