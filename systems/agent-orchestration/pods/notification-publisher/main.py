from __future__ import annotations

import os

import redis

from gops_agents.events.publisher import RedisNotificationPublisher


def main() -> None:
    from alfaka.common.kafka_io import create_json_consumer

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    decisions_topic = os.getenv("AGENT_NOTIFICATION_DECISIONS_TOPIC", "agents.notification-decisions.v1")
    risk_topic = os.getenv("RISK_EVENTS_TOPIC", "risk.events.v1")
    topics = [topic for topic in dict.fromkeys([decisions_topic, risk_topic]) if topic]
    group_id = os.getenv("AGENT_NOTIFICATION_PUBLISHER_GROUP_ID", "gops-agent-notification-publisher")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    consumer = create_json_consumer(topics, kafka_servers, group_id, "gops-agent-notification-publisher")
    publisher = RedisNotificationPublisher(redis.from_url(redis_url, decode_responses=True))
    print(f"Agent notification publisher started: topics={topics} redis={redis_url}", flush=True)
    for record in consumer:
        payload = publisher.publish(record.value)
        print(f"Agent alert published: symbol={payload.get('symbol')} level={payload.get('level')}", flush=True)


if __name__ == "__main__":
    main()
