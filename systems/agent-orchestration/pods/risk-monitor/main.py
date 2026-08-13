"""Risk monitor pod — consumes candles + fills, publishes risk.events.v1.

Env:
  KAFKA_BOOTSTRAP_SERVERS      default localhost:9092
  RISK_MONITOR_INPUT_TOPICS    csv, default candles(1m,5m,1d) + orders.fills.v1
  RISK_EVENTS_TOPIC            default risk.events.v1
  RISK_MONITOR_GROUP_ID        default gops-risk-monitor
  RISK_MONITOR_EQUITY          optional account equity (float); unset = equity
                               rules (concentration, daily loss) stay silent
"""

from __future__ import annotations

import os

from gops_agents.risk import RiskMonitor


DEFAULT_INPUT_TOPICS = ",".join([
    "market.layer.candles.1m.closed.v1",
    "market.layer.candles.5m.closed.v1",
    "market.layer.candles.1d.closed.v1",
    "orders.fills.v1",
])


def main() -> None:
    from market_data.common.kafka_io import create_json_consumer, create_json_producer

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    input_topics = parse_csv(os.getenv("RISK_MONITOR_INPUT_TOPICS", DEFAULT_INPUT_TOPICS))
    output_topic = os.getenv("RISK_EVENTS_TOPIC", "risk.events.v1")
    group_id = os.getenv("RISK_MONITOR_GROUP_ID", "gops-risk-monitor")
    monitor = RiskMonitor(account_equity=parse_float(os.getenv("RISK_MONITOR_EQUITY")))
    consumer = create_json_consumer(input_topics, kafka_servers, group_id, "gops-risk-monitor")
    producer = create_json_producer(kafka_servers, "gops-risk-monitor")
    print(f"Risk monitor started: input_topics={input_topics} output_topic={output_topic}", flush=True)
    for record in consumer:
        for event in monitor.handle(record.value, record.topic):
            producer.send(output_topic, key=event.get("symbol"), value=event)
            print(
                f"Risk event published: {event.get('eventId')} {event.get('symbol')} {event.get('eventType')}",
                flush=True,
            )


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


if __name__ == "__main__":
    main()
