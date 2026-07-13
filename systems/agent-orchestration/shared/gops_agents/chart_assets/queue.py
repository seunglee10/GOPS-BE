from __future__ import annotations

from collections import deque
from typing import Any

from .envelope import ChartAssetBuildEnvelope
from .job_store import PostgresChartAssetJobStore


class InMemoryChartAssetBuildQueue:
    def __init__(self):
        self.items: deque[dict[str, Any]] = deque()

    def submit(self, envelope: ChartAssetBuildEnvelope) -> None:
        self.items.append(envelope.to_dict())


class PostgresChartAssetBuildQueue:
    def __init__(self, store: PostgresChartAssetJobStore | None = None):
        self.store = store or PostgresChartAssetJobStore()

    def submit(self, envelope: ChartAssetBuildEnvelope) -> None:
        self.store.enqueue(envelope)

    def claim_next(self, worker_id: str, *, lease_seconds: int = 900) -> dict[str, Any] | None:
        return self.store.claim_next(worker_id, lease_seconds=lease_seconds)


_memory_queue = InMemoryChartAssetBuildQueue()


def build_chart_asset_queue_from_env():
    return PostgresChartAssetBuildQueue()
