# 역할: Kafka Producer/Consumer를 JSON 직렬화 방식으로 생성합니다.
# 사용: Alpaca 수집기, 스트리밍 처리기, S3 sink가 같은 Kafka 연결 방식을 공유합니다.
# 설정: KAFKA_BOOTSTRAP_SERVERS와 KAFKA_AUTO_OFFSET_RESET을 사용합니다.
import json
import os

from alfaka.common.env import parse_csv


def create_json_producer(bootstrap_servers, client_id):
    from kafka import KafkaProducer

    return KafkaProducer(
        bootstrap_servers=parse_csv(bootstrap_servers),
        client_id=client_id,
        key_serializer=lambda value: value.encode("utf-8"),
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def create_json_consumer(
    topics,
    bootstrap_servers,
    group_id,
    client_id,
    enable_auto_commit=None,
    max_poll_interval_ms=None,
    max_poll_records=None,
    session_timeout_ms=None,
    heartbeat_interval_ms=None,
):
    from kafka import KafkaConsumer

    if enable_auto_commit is None:
        enable_auto_commit = os.getenv("KAFKA_ENABLE_AUTO_COMMIT", "true").lower() in {"1", "true", "yes"}

    options = {
        "bootstrap_servers": parse_csv(bootstrap_servers),
        "group_id": group_id,
        "client_id": client_id,
        "auto_offset_reset": os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest"),
        "enable_auto_commit": enable_auto_commit,
        "key_deserializer": lambda value: value.decode("utf-8") if value else None,
        "value_deserializer": lambda value: json.loads(value.decode("utf-8")),
    }
    for key, value in {
        "max_poll_interval_ms": max_poll_interval_ms,
        "max_poll_records": max_poll_records,
        "session_timeout_ms": session_timeout_ms,
        "heartbeat_interval_ms": heartbeat_interval_ms,
    }.items():
        parsed = optional_positive_int(value)
        if parsed is not None:
            options[key] = parsed

    return KafkaConsumer(*topics, **options)


def optional_positive_int(value):
    if value is None or value == "":
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None
