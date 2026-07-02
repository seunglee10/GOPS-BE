from __future__ import annotations

import os
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .request_envelope import AgentAnalysisRequestEnvelope


DEFAULT_ANALYSIS_REQUESTS_TOPIC = "agents.analysis-requests.v1"
DEFAULT_DEEP_ANALYSIS_REQUESTS_TOPIC = "agents.deep-analysis-requests.v1"


@dataclass
class AnalysisQueueMetrics:
    backend: str
    topic: str | None = None
    queue_depth: int | None = None
    consumer_lag: int | None = None
    producer_buffered: int | None = None
    available: bool = True
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class AnalysisRequestQueue:
    def submit(self, envelope: AgentAnalysisRequestEnvelope) -> None:
        raise NotImplementedError

    def metrics(self) -> AnalysisQueueMetrics:
        return AnalysisQueueMetrics(backend="unknown", available=False, reason="queue_metrics_unavailable")


class InProcessAnalysisRequestQueue(AnalysisRequestQueue):
    def __init__(self):
        self.items: deque[dict[str, Any]] = deque()

    def submit(self, envelope: AgentAnalysisRequestEnvelope) -> None:
        self.items.append(envelope.to_dict())

    def pop(self) -> dict[str, Any] | None:
        return self.items.popleft() if self.items else None

    def metrics(self) -> AnalysisQueueMetrics:
        return AnalysisQueueMetrics(backend="in-process", queue_depth=len(self.items), consumer_lag=0)


@dataclass
class KafkaAnalysisRequestQueue(AnalysisRequestQueue):
    producer: Any
    topic: str = DEFAULT_ANALYSIS_REQUESTS_TOPIC

    def submit(self, envelope: AgentAnalysisRequestEnvelope) -> None:
        self.producer.send(self.topic, key=envelope.request_id, value=envelope.to_dict())
        self.producer.flush(timeout=float(os.getenv("AGENT_QUEUE_PRODUCER_FLUSH_SECONDS", "3")))

    def metrics(self) -> AnalysisQueueMetrics:
        buffered = None
        try:
            buffered = len(self.producer) if hasattr(self.producer, "__len__") else None
        except Exception:
            buffered = None
        return AnalysisQueueMetrics(
            backend="kafka",
            topic=self.topic,
            queue_depth=None,
            consumer_lag=None,
            producer_buffered=buffered,
            available=True,
            reason="broker_queue_depth_requires_consumer_group_metrics",
        )


def build_analysis_request_queue_from_env() -> AnalysisRequestQueue:
    return build_analysis_request_queue(
        topic_env="AGENT_ANALYSIS_REQUESTS_TOPIC",
        client_env="AGENT_ANALYSIS_QUEUE_CLIENT_ID",
        default_topic=DEFAULT_ANALYSIS_REQUESTS_TOPIC,
        default_client="gops-agent-analysis-admission",
    )


def build_deep_analysis_request_queue_from_env() -> AnalysisRequestQueue:
    return build_analysis_request_queue(
        topic_env="AGENT_DEEP_ANALYSIS_REQUESTS_TOPIC",
        client_env="AGENT_DEEP_ANALYSIS_QUEUE_CLIENT_ID",
        default_topic=DEFAULT_DEEP_ANALYSIS_REQUESTS_TOPIC,
        default_client="gops-agent-deep-analysis-admission",
    )


def build_analysis_request_queue(
    *,
    topic_env: str,
    client_env: str,
    default_topic: str,
    default_client: str,
) -> AnalysisRequestQueue:
    backend = os.getenv("AGENT_ANALYSIS_QUEUE_BACKEND", "auto").strip().lower()
    if backend in {"memory", "in-process", "inprocess", "local"}:
        return InProcessAnalysisRequestQueue()
    if backend in {"kafka", "auto"} and os.getenv("KAFKA_BOOTSTRAP_SERVERS"):
        try:
            from alfaka.common.kafka_io import create_json_producer

            return KafkaAnalysisRequestQueue(
                producer=create_json_producer(
                    os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
                    os.getenv(client_env, default_client),
                ),
                topic=os.getenv(topic_env, default_topic),
            )
        except Exception:
            if backend == "kafka":
                raise
    return InProcessAnalysisRequestQueue()
