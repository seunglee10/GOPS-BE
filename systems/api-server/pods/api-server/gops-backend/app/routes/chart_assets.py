from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from functools import lru_cache
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.services.alfaka_market_data import configured_universe_symbols, normalize_market_symbol, sp500_universe_symbols
from gops_agents.chart_assets.envelope import ALLOWED_INTERVALS, ChartAssetBuildEnvelope, utc_now_iso
from gops_agents.chart_assets.progress import TERMINAL_STATUSES, build_progress_store_from_env
from gops_agents.chart_assets.queue import build_chart_asset_queue_from_env
from gops_agents.chart_assets.storage import ChartAssetStorage


router = APIRouter()
JOB_ID_PATTERN = r"^cab-[A-Za-z0-9-]{8,64}$"


class ChartAssetBuildRequest(BaseModel):
    symbols: list[str] | Literal["sp500"]
    intervals: list[str] = Field(default_factory=lambda: list(ALLOWED_INTERVALS))
    llmEnabled: bool = True
    skipFreshHours: int = Field(default=0, ge=0, le=24 * 365)

    @field_validator("intervals")
    @classmethod
    def validate_intervals(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in value))
        if not normalized or set(normalized).difference(ALLOWED_INTERVALS):
            raise ValueError("intervals must contain only 1D, 1W, and 1M")
        return normalized


@router.get("/api/charts/analysis-assets")
def chart_analysis_assets(symbol: str = Query(min_length=1, max_length=12)) -> dict[str, Any]:
    normalized = normalize_market_symbol(symbol)
    try:
        assets = chart_asset_storage().get_symbol_assets(normalized)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis asset storage is unavailable.") from exc
    return {"symbol": normalized, "assets": assets, "meta": {"servedAt": utc_now_iso()}}


@router.get("/api/charts/analysis-assets/coverage")
def chart_analysis_asset_coverage(symbols: str | None = Query(default=None, max_length=4096)) -> dict[str, Any]:
    selected = _parse_symbol_csv(symbols) if symbols else None
    try:
        items = chart_asset_storage().coverage(selected)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis asset coverage is unavailable.") from exc
    return {"items": items, "total": len(items)}


@router.post("/api/charts/analysis-assets/build", status_code=202)
def build_chart_analysis_assets(
    request: ChartAssetBuildRequest,
    response: Response,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    symbols = _requested_symbols(request.symbols)
    envelope = ChartAssetBuildEnvelope.create(
        requested_by=hashlib.sha256(user.sub.encode("utf-8")).hexdigest()[:24],
        symbols=symbols,
        intervals=request.intervals,
        llm_enabled=request.llmEnabled,
        skip_fresh_hours=request.skipFreshHours,
    )
    progress = chart_asset_progress_store()
    progress.initialize(envelope)
    try:
        chart_asset_build_queue().submit(envelope)
    except Exception as exc:
        progress.set_status(envelope.job_id, "failed", finishedAt=utc_now_iso())
        raise HTTPException(status_code=503, detail="Chart analysis asset build queue is unavailable.") from exc
    response.status_code = 202
    return {
        "jobId": envelope.job_id,
        "status": "queued",
        "status_url": f"/api/charts/analysis-assets/build/{envelope.job_id}",
        "stream_url": f"/api/charts/analysis-assets/build/{envelope.job_id}/stream",
    }


@router.get("/api/charts/analysis-assets/build/{job_id}")
def chart_analysis_asset_build_status(
    job_id: str = Path(min_length=12, max_length=80, pattern=JOB_ID_PATTERN),
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    state = chart_asset_progress_store().get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Chart analysis asset build job not found.")
    return state


@router.get("/api/charts/analysis-assets/build/{job_id}/stream")
def chart_analysis_asset_build_stream(
    job_id: str = Path(min_length=12, max_length=80, pattern=JOB_ID_PATTERN),
    _user: AuthenticatedUser = Depends(require_current_user),
) -> StreamingResponse:
    if chart_asset_progress_store().get(job_id) is None:
        raise HTTPException(status_code=404, detail="Chart analysis asset build job not found.")
    return StreamingResponse(_stream_build_updates(job_id), media_type="text/event-stream")


@router.post("/api/charts/analysis-assets/build/{job_id}/cancel")
def cancel_chart_analysis_asset_build(
    job_id: str = Path(min_length=12, max_length=80, pattern=JOB_ID_PATTERN),
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    state = chart_asset_progress_store().request_cancel(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Chart analysis asset build job not found.")
    return state


async def _stream_build_updates(job_id: str):
    store = chart_asset_progress_store()
    pubsub = store.pubsub(job_id)
    deadline = time.monotonic() + 3600
    last_snapshot = None
    try:
        while time.monotonic() < deadline:
            if pubsub is not None:
                message = await asyncio.to_thread(pubsub.get_message, timeout=0.25)
                if message and message.get("type") == "message":
                    event = _json_object(message.get("data"))
                    yield _sse("update", event)
            state = store.get(job_id)
            if state is None:
                yield _sse("error", {"jobId": job_id, "detail": "job state expired"})
                return
            fingerprint = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if fingerprint != last_snapshot:
                last_snapshot = fingerprint
                yield _sse("status", state)
            if state.get("status") in TERMINAL_STATUSES:
                return
            await asyncio.sleep(0.5)
        yield _sse("timeout", {"jobId": job_id})
    finally:
        if pubsub is not None:
            pubsub.close()


def _requested_symbols(value: list[str] | str) -> list[str]:
    if value == "sp500":
        symbols = list(sp500_universe_symbols(fallback_to_configured=False))
        if not symbols:
            raise HTTPException(status_code=503, detail="S&P 500 universe registry is unavailable.")
        return symbols
    registry = set(sp500_universe_symbols()) | set(configured_universe_symbols())
    symbols = list(dict.fromkeys(normalize_market_symbol(item) for item in value))
    if not symbols:
        raise HTTPException(status_code=400, detail="At least one symbol is required.")
    unsupported = [symbol for symbol in symbols if symbol not in registry]
    if unsupported:
        raise HTTPException(status_code=400, detail=f"Unsupported chart symbols: {','.join(unsupported)}")
    return symbols


def _parse_symbol_csv(value: str) -> list[str]:
    raw = [item for item in re.split(r"[\s,]+", value) if item]
    return list(dict.fromkeys(normalize_market_symbol(item) for item in raw))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict): return value
    if isinstance(value, bytes): value = value.decode("utf-8")
    try: parsed = json.loads(str(value))
    except ValueError: return {}
    return parsed if isinstance(parsed, dict) else {}


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@lru_cache(maxsize=1)
def chart_asset_storage() -> ChartAssetStorage:
    return ChartAssetStorage()


@lru_cache(maxsize=1)
def chart_asset_progress_store():
    return build_progress_store_from_env()


@lru_cache(maxsize=1)
def chart_asset_build_queue():
    return build_chart_asset_queue_from_env()
