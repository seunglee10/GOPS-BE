from __future__ import annotations

import os

from gops_agents.event_detector import MarketEventDetector


def main() -> None:
    from alfaka.common.kafka_io import create_json_consumer, create_json_producer

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    input_topics = parse_csv(os.getenv(
        "AGENT_EVENT_INPUT_TOPICS",
        "market.ticks.v1,market.candles.closed.v1",
    ))
    output_topic = os.getenv("AGENT_MARKET_EVENTS_TOPIC", "agents.market-events.v1")
    group_id = os.getenv("AGENT_EVENT_DETECTOR_GROUP_ID", "gops-agent-event-detector")
    detector = MarketEventDetector()
    consumer = create_json_consumer(input_topics, kafka_servers, group_id, "gops-agent-event-detector")
    producer = create_json_producer(kafka_servers, "gops-agent-event-detector")
    print(f"Agent event detector started: input_topics={input_topics} output_topic={output_topic}", flush=True)
    for record in consumer:
        for event in detector.detect(record.value, record.topic):
            producer.send(output_topic, key=event.symbol, value=event.to_dict())
            print(f"Agent market event published: {event.eventId} {event.symbol} {event.eventType}", flush=True)


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    main()
