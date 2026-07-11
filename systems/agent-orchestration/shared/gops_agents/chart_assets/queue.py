from __future__ import annotations

import os
from collections import deque
from typing import Any

from .envelope import ChartAssetBuildEnvelope


DEFAULT_TOPIC = "agents.chart-asset-build-requests.v1"


class InMemoryChartAssetBuildQueue:
    def __init__(self):
        self.items: deque[dict[str, Any]] = deque()

    def submit(self, envelope: ChartAssetBuildEnvelope) -> None:
        self.items.append(envelope.to_dict())


class KafkaChartAssetBuildQueue:
    def __init__(self, producer: Any, topic: str = DEFAULT_TOPIC):
        self.producer = producer
        self.topic = topic

    def submit(self, envelope: ChartAssetBuildEnvelope) -> None:
        self.producer.send(self.topic, key=envelope.job_id, value=envelope.to_dict())
        self.producer.flush(timeout=float(os.getenv("CHART_ASSET_QUEUE_FLUSH_SECONDS", "3")))


_memory_queue = InMemoryChartAssetBuildQueue()


def build_chart_asset_queue_from_env():
    if os.getenv("KAFKA_BOOTSTRAP_SERVERS"):
        from alfaka.common.kafka_io import create_json_producer
        return KafkaChartAssetBuildQueue(
            create_json_producer(os.getenv("KAFKA_BOOTSTRAP_SERVERS"), "gops-chart-asset-build-api"),
            os.getenv("CHART_ASSET_BUILD_REQUESTS_TOPIC", DEFAULT_TOPIC),
        )
    return _memory_queue
