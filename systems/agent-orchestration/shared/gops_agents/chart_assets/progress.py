from __future__ import annotations

import copy
import threading
from typing import Any

from .envelope import ChartAssetBuildEnvelope, utc_now_iso
from .job_store import PostgresChartAssetJobStore, TERMINAL_JOB_STATUSES


TERMINAL_STATUSES = TERMINAL_JOB_STATUSES


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


class PostgresChartAssetProgressStore:
    def __init__(self, store: PostgresChartAssetJobStore | None = None):
        self.store = store or PostgresChartAssetJobStore()

    def initialize(self, envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
        return self.store.enqueue(envelope)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get(job_id)

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        return self.store.request_cancel(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        return self.store.is_cancel_requested(job_id)

    def set_status(self, job_id: str, status: str, **values: Any) -> dict[str, Any] | None:
        return self.store.set_status(job_id, status, **values)

    def add_log(self, job_id: str, message: str) -> None:
        self.store.add_log(job_id, message)

    def record_item(self, job_id: str, item: dict[str, Any]) -> None:
        self.store.record_item(job_id, item)

    def set_current(self, _job_id: str, _current: str) -> None:
        return None

    def record_repair(self, job_id: str, result: dict[str, Any]) -> None:
        self.store.record_repair(job_id, result)

    def pubsub(self, _job_id: str):
        return None


_memory_store = InMemoryChartAssetProgressStore()


def build_progress_store_from_env() -> InMemoryChartAssetProgressStore:
    return PostgresChartAssetProgressStore()


def initial_state(envelope: ChartAssetBuildEnvelope) -> dict[str, Any]:
    total = len(envelope.symbols) * len(envelope.intervals)
    return {
        "jobId": envelope.job_id,
        "status": "queued",
        "requested": {"symbolCount": len(envelope.symbols), "intervals": list(envelope.intervals), "force": envelope.force},
        "progress": {"total": total, "done": 0, "failed": 0, "skipped": 0, "warnings": 0, "current": None},
        "repair": initial_repair_state(),
        "recentItems": [], "failedItems": [], "startedAt": None, "finishedAt": None, "cancelRequested": False,
    }


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
