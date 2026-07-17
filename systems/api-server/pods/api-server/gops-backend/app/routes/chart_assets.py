from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.routes import charts as charts_routes
from app.routes.simulator import simulator_gateway_from_app
from app.services.alfaka_market_data import configured_universe_symbols, normalize_market_symbol, sp500_universe_symbols
from app.services.simulator_gateway import SimulatorUnavailable
from alfaka.analytics.analysis_candles import analysis_input_digest, compute_analysis_coverage, merge_canonical_candles
from alfaka.analytics import geometry as geometry_analysis
from gops_agents.chart_assets.envelope import ALLOWED_INTERVALS, BUILD_INTERVALS, ChartAssetBuildEnvelope, utc_now_iso
from gops_agents.chart_assets.progress import build_progress_store_from_env
from gops_agents.chart_assets.queue import build_chart_asset_queue_from_env
from gops_agents.chart_assets.storage import build_chart_asset_storage_from_env


router = APIRouter()
JOB_ID_PATTERN = r"^cab-[A-Za-z0-9-]{8,64}$"


class ChartAssetBuildRequest(BaseModel):
    symbols: list[str] | Literal["sp500"]
    intervals: list[str] = Field(default_factory=lambda: list(BUILD_INTERVALS))
    force: bool = False

    @field_validator("intervals")
    @classmethod
    def validate_intervals(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in value))
        if not normalized or set(normalized).difference(BUILD_INTERVALS):
            raise ValueError("chart asset builds support only 1m and 1D intervals")
        return normalized


@router.get("/api/charts/analysis-assets")
def chart_analysis_assets(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str | None = Query(default=None, pattern="^(1m|5m|10m|1h|4h|1D|1W)$"),
) -> dict[str, Any]:
    normalized = normalize_market_symbol(symbol)
    try:
        assets = chart_asset_storage().get_symbol_assets(normalized)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis asset storage is unavailable.") from exc
    meta: dict[str, Any] = {"servedAt": utc_now_iso()}
    try:
        simulator_status = simulator_gateway_from_app(request.app).status()
    except SimulatorUnavailable:
        simulator_status = {"mode": "live"}
    if simulator_status.get("mode") == "simulation":
        cutoff_value = str(simulator_status.get("virtualTime") or "")
        cutoff = _parse_timestamp(cutoff_value)
        assets = _assets_at_or_before(assets, cutoff)
        dynamic_status = "not_requested"
        if interval is not None:
            try:
                dynamic_asset = _build_simulation_analysis_asset(request, normalized, interval, cutoff)
                dynamic_status = "ready" if dynamic_asset is not None else "data_insufficient"
            except Exception:
                dynamic_asset = None
                dynamic_status = "unavailable"
            if dynamic_asset is not None:
                assets[interval] = dynamic_asset
        meta.update({
            "simulation": True,
            "cutoff": cutoff_value,
            "runId": simulator_status.get("runId"),
            "dynamicInterval": interval,
            "dynamicStatus": dynamic_status,
        })
    return {"symbol": normalized, "assets": assets, "meta": meta}


@router.get("/api/charts/analysis-assets/coverage")
def chart_analysis_asset_coverage(symbols: str | None = Query(default=None, max_length=4096)) -> dict[str, Any]:
    selected = _parse_symbol_csv(symbols) if symbols else None
    try:
        items = chart_asset_storage().coverage(selected)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis asset coverage is unavailable.") from exc
    return {"items": items, "total": len(items)}


@router.delete("/api/charts/analysis-assets")
def delete_chart_analysis_assets(
    symbols: str = Query(min_length=1, max_length=4096),
    intervals: str = Query(default=",".join(ALLOWED_INTERVALS), min_length=2, max_length=64),
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if _storage_maintenance_enabled():
        raise HTTPException(status_code=503, detail="Chart analysis asset storage migration is in progress.")
    selected_symbols = _parse_symbol_csv(symbols)
    selected_intervals = _parse_intervals_csv(intervals)
    if len(selected_symbols) > 100:
        raise HTTPException(status_code=400, detail="At most 100 symbols can be deleted at once.")
    try:
        deleted = chart_asset_storage().delete(selected_symbols, selected_intervals)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis assets could not be deleted.") from exc
    return {"symbols": selected_symbols, "intervals": selected_intervals, "deleted": deleted}


@router.post("/api/charts/analysis-assets/build", status_code=202)
def build_chart_analysis_assets(
    request: ChartAssetBuildRequest,
    response: Response,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if _storage_maintenance_enabled():
        raise HTTPException(status_code=503, detail="Chart analysis asset storage migration is in progress.")
    if request.symbols == "sp500" and request.force:
        raise HTTPException(status_code=400, detail="S&P 500 force refresh is not supported.")
    symbols = _requested_symbols(request.symbols)
    envelope = ChartAssetBuildEnvelope.create(
        requested_by=hashlib.sha256(user.sub.encode("utf-8")).hexdigest()[:24],
        symbols=symbols,
        intervals=request.intervals,
        force=request.force,
        source="manual",
    )
    progress = chart_asset_progress_store()
    state = progress.initialize(envelope)
    actual_job_id = str(state.get("jobId") or envelope.job_id)
    coalesced = actual_job_id != envelope.job_id
    try:
        if not coalesced:
            submission = chart_asset_build_queue().submit(envelope)
            actual_job_id = str((submission or {}).get("jobId") or envelope.job_id)
            coalesced = actual_job_id != envelope.job_id
    except Exception as exc:
        progress.set_status(envelope.job_id, "failed", finishedAt=utc_now_iso())
        raise HTTPException(status_code=503, detail="Chart analysis asset build queue is unavailable.") from exc
    response.status_code = 202
    return {
        "jobId": actual_job_id,
        "status": "queued",
        "status_url": f"/api/charts/analysis-assets/build/{actual_job_id}",
        "coalesced": coalesced,
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


@router.post("/api/charts/analysis-assets/build/{job_id}/cancel")
def cancel_chart_analysis_asset_build(
    job_id: str = Path(min_length=12, max_length=80, pattern=JOB_ID_PATTERN),
    _user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    state = chart_asset_progress_store().request_cancel(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Chart analysis asset build job not found.")
    return state


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


def _parse_intervals_csv(value: str) -> list[str]:
    intervals = list(dict.fromkeys(item for item in re.split(r"[\s,]+", value) if item))
    if not intervals or set(intervals).difference(ALLOWED_INTERVALS):
        raise HTTPException(status_code=400, detail="intervals must contain only supported chart intervals")
    return intervals


def _assets_at_or_before(
    assets: dict[str, dict[str, Any] | None],
    cutoff: datetime | None,
) -> dict[str, dict[str, Any] | None]:
    filtered = {interval: None for interval in ALLOWED_INTERVALS}
    if cutoff is None:
        return filtered
    for interval in ALLOWED_INTERVALS:
        asset = assets.get(interval)
        as_of = _parse_timestamp(asset.get("asOf")) if isinstance(asset, dict) else None
        if as_of is not None and as_of <= cutoff:
            filtered[interval] = asset
    return filtered


def _build_simulation_analysis_asset(
    request: Request,
    symbol: str,
    interval: str,
    cutoff: datetime | None,
) -> dict[str, Any] | None:
    if cutoff is None:
        return None
    limit = charts_routes.PUBLIC_CHART_CANDLE_LIMIT
    replay = simulator_gateway_from_app(request.app).candles(symbol, interval, limit)
    historical = charts_routes.get_query_service().candle_snapshot(
        symbol,
        interval,
        "",
        limit,
        to_time=charts_routes.SIMULATION_REPLAY_START.isoformat().replace("+00:00", "Z"),
    )
    merged = charts_routes._merge_simulation_candles(historical, replay, limit)
    rows = merge_canonical_candles(
        (
            dict(row)
            for row in merged.get("candles") or []
            if isinstance(row, dict)
            and row.get("isClosed", row.get("is_closed", True)) is not False
            and (_parse_timestamp(row.get("timestamp")) or datetime.max.replace(tzinfo=UTC)) <= cutoff
        ),
        interval=interval,
        view="chart_completed",
    )
    target_bars = geometry_analysis.TARGET_BARS[interval]
    rows = rows[-target_bars:]
    coverage = compute_analysis_coverage(rows, interval, display_bars=target_bars, now=cutoff)
    if len(rows) < geometry_analysis.MINIMUM_BARS or coverage.get("coverageState") == "data_insufficient":
        return None
    result = geometry_analysis.analyze_geometry(symbol, interval, rows)
    geometry = {
        "drawings": result["drawings"],
        "supports": result["supports"],
        "resistances": result["resistances"],
        "patterns": result["patterns"],
        "primaryPattern": result["primaryPattern"],
        "tradePlan": result["tradePlan"],
        "primaryTriangle": result["primaryTriangle"],
        "historicalTriangle": result["historicalTriangle"],
        "evidence": result["evidence"],
    }
    for optional_field in ("trends", "primaryTrend", "drawingGroups", "analysisTrace"):
        if optional_field in result:
            geometry[optional_field] = result[optional_field]
    as_of = str(rows[-1]["timestamp"])
    actual_bars = len(rows)
    return {
        "assetVersion": "geometry",
        "algorithmVersion": geometry_analysis.ALGORITHM_VERSION,
        "symbol": symbol,
        "interval": interval,
        "sourceInterval": interval,
        "asOf": as_of,
        "generatedAt": utc_now_iso(),
        "status": "ready",
        "inputDigest": analysis_input_digest(symbol, interval, rows),
        "coverage": {
            "state": "full" if actual_bars >= target_bars and coverage.get("missingBars") == 0 else "partial",
            "targetBars": target_bars,
            "actualBars": actual_bars,
            "contiguousBars": int(coverage.get("recentContiguousBars") or 0),
            "missingBars": int(coverage.get("missingBars") or 0),
            "lastExpectedClosedAt": coverage.get("lastExpectedClosedAt"),
            "lastActualClosedAt": coverage.get("lastActualClosedAt") or as_of,
            "qualityFlags": [*list(coverage.get("qualityFlags") or []), "simulation_replay"],
        },
        "geometry": geometry,
        "indicators": result["indicators"],
    }


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@lru_cache(maxsize=1)
def chart_asset_storage():
    return build_chart_asset_storage_from_env()


@lru_cache(maxsize=1)
def chart_asset_progress_store():
    return build_progress_store_from_env()


@lru_cache(maxsize=1)
def chart_asset_build_queue():
    return build_chart_asset_queue_from_env()


def _storage_maintenance_enabled() -> bool:
    return os.getenv("CHART_ASSET_STORAGE_MAINTENANCE", "false").strip().lower() in {"1", "true", "yes", "on"}
