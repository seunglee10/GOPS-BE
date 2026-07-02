from __future__ import annotations

import json
import os
from typing import Any

from .report_store import ReportStore, analysis_report_from_dict, build_report_store_from_env


DEFAULT_REPORT_UPDATES_CHANNEL = "agent.reports"


class AgentDeliveryGateway:
    def __init__(self, *, store: ReportStore | None = None, redis_client=None):
        self.store = store or build_report_store_from_env()
        self.redis = redis_client

    def process_report_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        report = analysis_report_from_dict(payload)
        if report is None:
            return {"status": "skipped", "reason": "invalid-report"}
        self.store.save(report)
        self.publish_report_update(report.to_dict())
        return {"status": "ok", "analysisId": report.analysisId, "reportStatus": report.status}

    def publish_report_update(self, payload: dict[str, Any]) -> None:
        if os.getenv("AGENT_DELIVERY_REDIS_PUBLISH_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
            return
        redis_client = self.redis or default_redis_client()
        if redis_client is None:
            return
        channel_base = os.getenv("AGENT_REPORT_UPDATES_CHANNEL", DEFAULT_REPORT_UPDATES_CHANNEL)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        try:
            redis_client.publish(channel_base, encoded)
            redis_client.publish(f"{channel_base}:{payload.get('analysisId')}", encoded)
        except Exception:
            return


def default_redis_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    import redis

    return redis.from_url(redis_url, decode_responses=True)


def run_delivery_gateway() -> None:
    from alfaka.common.kafka_io import create_json_consumer

    topic = os.getenv("AGENT_ANALYSIS_RESULTS_TOPIC", "agents.analysis-results.v1")
    consumer = create_json_consumer(
        [topic],
        os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        os.getenv("AGENT_DELIVERY_GATEWAY_GROUP_ID", "gops-agent-delivery-gateway"),
        os.getenv("AGENT_DELIVERY_GATEWAY_CLIENT_ID", "gops-agent-delivery-gateway"),
        enable_auto_commit=False,
        max_poll_records=os.getenv("AGENT_DELIVERY_GATEWAY_MAX_POLL_RECORDS", "10"),
    )
    gateway = AgentDeliveryGateway()
    max_messages = positive_int_or_none(os.getenv("AGENT_DELIVERY_GATEWAY_MAX_MESSAGES"))
    processed = 0
    try:
        for message in consumer:
            gateway.process_report_payload(message.value)
            consumer.commit()
            processed += 1
            if max_messages is not None and processed >= max_messages:
                break
    finally:
        consumer.close()


def positive_int_or_none(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else 0
    except Exception:
        return None
    return parsed if parsed > 0 else None
