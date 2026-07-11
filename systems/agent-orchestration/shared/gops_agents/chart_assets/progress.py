from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

from .envelope import ChartAssetBuildEnvelope, utc_now_iso


STATUS_KEY_PREFIX = "gops:chart-assets:build"
CHANNEL_PREFIX = "chart-assets.build"
STATUS_TTL_SECONDS = 86400
TERMINAL_STATUSES = {"completed", "completed_with_warnings", "completed_with_errors", "failed", "canceled"}


class InMemoryChartAssetProgressStore:
    def __init__(self):
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def initialize(self, envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
        state = initial_state(envelope)
        return self.save(state, event={"type": "status", "status": "queued"})

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(job_id)
            return copy.deepcopy(state) if state else None

    def save(self, state: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._states[state["jobId"]] = copy.deepcopy(state)
        return copy.deepcopy(state)

    def mutate(self, job_id: str, mutator, *, event: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(job_id)
            if state is None:
                return None
            mutator(state)
            return self.save(state, event=event)

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        return self.mutate(job_id, lambda state: state.update(cancelRequested=True), event={"type": "status", "cancelRequested": True})

    def is_cancel_requested(self, job_id: str) -> bool:
        state = self.get(job_id)
        return bool(state and state.get("cancelRequested"))

    def set_status(self, job_id: str, status: str, **values: Any) -> dict[str, Any] | None:
        def mutate(state: dict[str, Any]) -> None:
            state["status"] = status
            state.update(values)
        return self.mutate(job_id, mutate, event={"type": "status", "status": status})

    def add_log(self, job_id: str, message: str) -> None:
        # The in-memory fallback has no streaming transport. Do not retain logs in
        # job state; operational details are intentionally ephemeral.
        return None

    def record_item(self, job_id: str, item: dict[str, Any]) -> None:
        def mutate(state: dict[str, Any]) -> None:
            status = item.get("status")
            progress = state["progress"]
            progress["done"] += 1
            if status == "failed": progress["failed"] += 1
            if status == "skipped": progress["skipped"] += 1
            if status == "saved_with_warning" or item.get("warning"): progress["warnings"] += 1
            progress["current"] = f"{item.get('symbol')}:{item.get('interval')}"
            state["recentItems"] = [*state.get("recentItems", []), copy.deepcopy(item)][-50:]
            if status == "failed":
                state["failedItems"] = [*state.get("failedItems", []), copy.deepcopy(item)]
        self.mutate(job_id, mutate, event={"type": "item", **item})

    def set_current(self, job_id: str, current: str) -> None:
        self.mutate(
            job_id,
            lambda state: state["progress"].update(current=current),
            event={"type": "status", "current": current},
        )

    def record_repair(self, job_id: str, result: dict[str, Any]) -> None:
        def mutate(state: dict[str, Any]) -> None:
            repair = state.setdefault("repair", initial_repair_state())
            if result.get("checked"):
                repair["checkedSymbols"] += 1
            if result.get("attempted"):
                repair["attemptedSymbols"] += 1
            if result.get("repaired"):
                repair["repairedSymbols"] += 1
            if result.get("unavailable"):
                repair["unavailableSymbols"] += 1
                state["progress"]["warnings"] += 1
            repair["missingBarsBefore"] += int(result.get("missing_before") or result.get("missingBefore") or 0)
            repair["missingBarsAfter"] += int(result.get("missing_after") or result.get("missingAfter") or 0)
            repair["materializedRows"] += int(result.get("materialized_rows") or result.get("materializedRows") or 0)
            reason = str(result.get("reason") or "").strip()
            if reason and reason not in {"coverage_complete", "repaired"}:
                reason_codes = repair.setdefault("reasonCodes", {})
                reason_codes[reason] = int(reason_codes.get(reason) or 0) + 1
        self.mutate(job_id, mutate, event={"type": "repair", "reason": result.get("reason")})

    def pubsub(self, job_id: str):
        return None


class RedisChartAssetProgressStore(InMemoryChartAssetProgressStore):
    def __init__(self, redis_client: Any | None = None):
        super().__init__()
        if redis_client is not None:
            self.redis = redis_client
        else:
            import redis
            self.redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            payload = self.redis.get(status_key(job_id))
        except Exception:
            return None
        if isinstance(payload, bytes): payload = payload.decode("utf-8")
        try:
            state = json.loads(payload) if payload else None
        except ValueError:
            return None
        return state if isinstance(state, dict) else None

    def add_log(self, job_id: str, message: str) -> None:
        try:
            self.redis.publish(
                channel_name(job_id),
                json.dumps({"type": "log", "jobId": job_id, "message": str(message)}, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            # Logging must never add a storage write or fail the build.
            return None

    def save(self, state: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        self.redis.setex(status_key(state["jobId"]), STATUS_TTL_SECONDS, encoded)
        if event is not None:
            self.redis.publish(channel_name(state["jobId"]), json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        return copy.deepcopy(state)

    def mutate(self, job_id: str, mutator, *, event: dict[str, Any] | None = None) -> dict[str, Any] | None:
        key = status_key(job_id)
        for _attempt in range(5):
            pipe = self.redis.pipeline()
            try:
                pipe.watch(key)
                payload = pipe.get(key)
                if isinstance(payload, bytes): payload = payload.decode("utf-8")
                state = json.loads(payload) if payload else None
                if not isinstance(state, dict):
                    pipe.unwatch()
                    return None
                mutator(state)
                pipe.multi()
                pipe.setex(key, STATUS_TTL_SECONDS, json.dumps(state, ensure_ascii=False, separators=(",", ":")))
                if event is not None:
                    pipe.publish(channel_name(job_id), json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                pipe.execute()
                return copy.deepcopy(state)
            except Exception as exc:
                if exc.__class__.__name__ != "WatchError":
                    raise
            finally:
                pipe.reset()
        raise RuntimeError("Chart asset progress update contention exceeded retry budget.")

    def pubsub(self, job_id: str):
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel_name(job_id))
        return pubsub


_memory_store = InMemoryChartAssetProgressStore()


def build_progress_store_from_env() -> InMemoryChartAssetProgressStore:
    if os.getenv("REDIS_URL"):
        try:
            return RedisChartAssetProgressStore()
        except Exception:
            pass
    return _memory_store


def initial_state(envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
    total = len(envelope.symbols) * len(envelope.intervals)
    return {
        "jobId": envelope.job_id,
        "status": "queued",
        "requested": {"symbolCount": len(envelope.symbols), "intervals": list(envelope.intervals), "llmEnabled": envelope.llm_enabled, "force": envelope.force},
        "progress": {"total": total, "done": 0, "failed": 0, "skipped": 0, "warnings": 0, "current": None},
        "repair": initial_repair_state(),
        "recentItems": [], "failedItems": [], "startedAt": None, "finishedAt": None, "cancelRequested": False,
    }


def status_key(job_id: str) -> str:
    return f"{STATUS_KEY_PREFIX}:{job_id}"


def channel_name(job_id: str) -> str:
    return f"{CHANNEL_PREFIX}:{job_id}"


def initial_repair_state() -> dict[str, Any]:
    return {
        "checkedSymbols": 0,
        "attemptedSymbols": 0,
        "repairedSymbols": 0,
        "unavailableSymbols": 0,
        "missingBarsBefore": 0,
        "missingBarsAfter": 0,
        "materializedRows": 0,
        "reasonCodes": {},
    }
