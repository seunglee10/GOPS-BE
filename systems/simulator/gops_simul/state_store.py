"""Run-scoped Redis persistence for replay clocks and user ledgers."""

from __future__ import annotations

import json
from typing import Protocol


class ReplayStateStore(Protocol):
    def load_active(self) -> dict[str, object] | None: ...
    def save(self, run_id: str, payload: dict[str, object]) -> None: ...
    def delete(self, run_id: str) -> None: ...


class RedisReplayStateStore:
    ACTIVE_RUN_KEY = "simulator:replay:active-run"
    RUN_KEY_PREFIX = "simulator:replay:run:"

    def __init__(self, client) -> None:
        self.client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisReplayStateStore":
        import redis
        return cls(redis.from_url(url, decode_responses=True))

    def load_active(self) -> dict[str, object] | None:
        run_id = self.client.get(self.ACTIVE_RUN_KEY)
        if not run_id:
            return None
        raw = self.client.get(self._run_key(str(run_id)))
        if not raw:
            self.client.delete(self.ACTIVE_RUN_KEY)
            return None
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None

    def save(self, run_id: str, payload: dict[str, object]) -> None:
        body = json.dumps({**payload, "runId": run_id}, ensure_ascii=False, separators=(",", ":"))
        pipeline = self.client.pipeline(transaction=True)
        pipeline.set(self._run_key(run_id), body)
        pipeline.set(self.ACTIVE_RUN_KEY, run_id)
        pipeline.execute()

    def delete(self, run_id: str) -> None:
        pipeline = self.client.pipeline(transaction=True)
        pipeline.delete(self._run_key(run_id))
        if self.client.get(self.ACTIVE_RUN_KEY) == run_id:
            pipeline.delete(self.ACTIVE_RUN_KEY)
        pipeline.execute()

    @classmethod
    def _run_key(cls, run_id: str) -> str:
        return f"{cls.RUN_KEY_PREFIX}{run_id}"
