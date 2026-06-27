from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException

from alfaka.backfill.runner import BackfillRunner
from alfaka.backfill.status import ACTIVE_STATUSES, RedisBackfillStore


logger = logging.getLogger(__name__)


class BackfillService:
    def __init__(self, provider=None, store=None, runner_factory=None):
        self.provider = provider
        self.store = store or RedisBackfillStore(redis_client=getattr(getattr(provider, "redis_provider", None), "redis", None))
        self.runner_factory = runner_factory or (lambda: BackfillRunner(store=self.store))

    def snapshot_metadata(self, symbol: str, interval: str, has_candles: bool) -> dict[str, Any]:
        if has_candles:
            return {
                "dataStatus": "ready",
                "backfillStatus": "not_requested",
                "canBackfill": False,
                "message": None,
            }
        latest = self._latest_status(symbol, interval)
        backfill_status = latest.get("status") if latest else "not_requested"
        can_backfill = backfill_status not in ACTIVE_STATUSES and backfill_status != "unavailable"
        message = latest.get("error") if latest and latest.get("status") in {"failed", "unavailable"} else None
        return {
            "dataStatus": "empty",
            "backfillStatus": backfill_status,
            "canBackfill": can_backfill,
            "message": message or "No candle data is available for this symbol and interval.",
        }

    def request_backfill(self, symbol: str, interval: str, start: str | None = None, end: str | None = None, mode: str = "default") -> dict[str, Any]:
        execution_mode = resolve_execution_mode(mode)
        try:
            record, deduplicated = self.store.create_request(symbol, interval, start=start, end=end, mode=execution_mode)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backfill status store failed: {exc}") from exc

        if not deduplicated and execution_mode in {"sample-dev", "sync-dev"}:
            record = self.runner_factory().run(record)

        return summarize_status(record, deduplicated=deduplicated)

    def get_status(self, symbol: str, interval: str, request_id: str | None = None) -> dict[str, Any]:
        try:
            record = self.store.get_status(request_id) if request_id else self.store.latest_status(symbol, interval)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backfill status store failed: {exc}") from exc
        if not record:
            return {
                "symbol": symbol,
                "interval": interval,
                "requestId": None,
                "status": "not_requested",
                "range": None,
            }
        return summarize_status(record)

    def _latest_status(self, symbol: str, interval: str) -> dict[str, Any] | None:
        try:
            return self.store.latest_status(symbol, interval)
        except Exception:
            logger.warning("Backfill status lookup failed for %s %s.", symbol, interval, exc_info=True)
            return None


def resolve_execution_mode(mode: str | None) -> str:
    requested = (mode or "default").strip()
    allow_requested_mode = os.getenv("BACKFILL_ALLOW_REQUESTED_MODE", "false").lower() in {"1", "true", "yes"}
    if allow_requested_mode and requested and requested != "default":
        return requested
    return os.getenv("BACKFILL_EXECUTION_MODE", "queue")


def summarize_status(record: dict[str, Any], deduplicated: bool | None = None) -> dict[str, Any]:
    payload = {
        "symbol": record["symbol"],
        "interval": record["interval"],
        "requestId": record["requestId"],
        "status": record["status"],
        "range": record.get("range"),
        "requestedAt": record.get("requestedAt"),
        "updatedAt": record.get("updatedAt"),
        "startedAt": record.get("startedAt"),
        "finishedAt": record.get("finishedAt"),
        "error": record.get("error"),
        "result": record.get("result"),
    }
    if deduplicated is not None:
        payload["deduplicated"] = deduplicated
    return payload


def get_backfill_service(provider=None) -> BackfillService:
    return BackfillService(provider=provider)
