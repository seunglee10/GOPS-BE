from __future__ import annotations

from copy import deepcopy
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
from app.routes.simulator import simulator_gateway_from_app
from app.services.alfaka_market_data import configured_universe_symbols, normalize_market_symbol, sp500_universe_symbols
from app.services.simulator_gateway import SimulatorUnavailable
from alfaka.analytics.geometry import ALGORITHM_VERSION
from gops_agents.chart_assets.envelope import ALLOWED_INTERVALS, BUILD_INTERVALS, ChartAssetBuildEnvelope, utc_now_iso
from gops_agents.chart_assets.progress import build_progress_store_from_env
from gops_agents.chart_assets.queue import build_chart_asset_queue_from_env
from gops_agents.chart_assets.storage import build_chart_asset_storage_from_env


router = APIRouter()
JOB_ID_PATTERN = r"^cab-[A-Za-z0-9-]{8,64}$"
# Requested demo-only exception. It is projected in the SIM response and is never persisted.
NVDA_SIMULATION_DEMO_AS_OF = "2026-07-14T04:00:00.000Z"
NVDA_SIMULATION_DEMO_DATASET_ID = "sp500-full-20260715-kst-v3"
NVDA_SIMULATION_DEMO_SYMBOL = "NVDA"
NVDA_SIMULATION_DEMO_INTERVAL = "1D"
NVDA_SIMULATION_DEMO_TRADE_PLAN_REASON = "simulation_demo_reward_risk_override"


class ChartAssetBuildRequest(BaseModel):
    symbols: list[str] | Literal["sp500"]
    intervals: list[str] = Field(default_factory=lambda: list(BUILD_INTERVALS))
    force: bool = False
    target: Literal["live", "simulation"] = "live"

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
    simulation = _simulation_asset_context(request)
    try:
        storage = chart_asset_storage()
        if simulation:
            assets = (
                {interval: storage.get_snapshot(simulation["datasetId"], normalized, interval, simulation["virtualTime"])}
                if interval
                else storage.get_symbol_snapshots(simulation["datasetId"], normalized, simulation["virtualTime"])
            )
        else:
            assets = {interval: storage.get(normalized, interval)} if interval else storage.get_symbol_assets(normalized)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis asset storage is unavailable.") from exc
    meta: dict[str, Any] = {"servedAt": utc_now_iso(), "assetContext": "simulation" if simulation else "live"}
    if simulation:
        raw_assets = assets
        demo_override = False
        if (
            simulation["datasetId"] == NVDA_SIMULATION_DEMO_DATASET_ID
            and normalized == NVDA_SIMULATION_DEMO_SYMBOL
            and interval == NVDA_SIMULATION_DEMO_INTERVAL
        ):
            try:
                persisted_demo_asset = storage.get(normalized, interval)
            except Exception:
                persisted_demo_asset = None
            demo_asset = _build_nvda_simulation_demo_asset(
                symbol=normalized,
                interval=interval,
                persisted_asset=persisted_demo_asset,
                cutoff=_parse_timestamp(simulation["virtualTime"]),
            )
            if demo_asset is not None and demo_asset.get("algorithmVersion") == ALGORITHM_VERSION:
                raw_assets = {**raw_assets, interval: demo_asset}
                demo_override = True
        assets = {
            key: asset if isinstance(asset, dict) and asset.get("algorithmVersion") == ALGORITHM_VERSION else None
            for key, asset in raw_assets.items()
        }
        selected = raw_assets.get(interval) if interval else next((asset for asset in raw_assets.values() if asset), None)
        snapshot_status = (
            "missing" if selected is None
            else "ready" if selected.get("algorithmVersion") == ALGORITHM_VERSION
            else "regeneration_required"
        )
        meta.update({
            "simulation": True,
            "cutoff": simulation["virtualTime"],
            "runId": simulation.get("runId"),
            "datasetId": simulation["datasetId"],
            "snapshotCutoff": simulation["snapshotCutoff"],
            "snapshotStatus": snapshot_status,
            "demoOverride": demo_override,
            "demoOverrideAsOf": NVDA_SIMULATION_DEMO_AS_OF if demo_override else None,
        })
    return {"symbol": normalized, "assets": assets, "meta": meta}


@router.get("/api/charts/analysis-assets/commentary")
def chart_analysis_asset_commentary(
    request: Request,
    symbol: str = Query(min_length=1, max_length=12),
    interval: str = Query(pattern="^(1m|5m|10m|1h|4h|1D|1W)$"),
) -> dict[str, Any]:
    normalized = normalize_market_symbol(symbol)
    simulation = _simulation_asset_context(request)
    try:
        storage = chart_asset_storage()
        asset = (
            storage.get_snapshot_commentary(simulation["datasetId"], normalized, interval, simulation["virtualTime"])
            if simulation
            else storage.get_commentary(normalized, interval)
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart commentary asset storage is unavailable.") from exc
    meta: dict[str, Any] = {"servedAt": utc_now_iso(), "assetContext": "simulation" if simulation else "live"}
    if simulation:
        snapshot_status = (
            "missing" if asset is None
            else "ready" if asset.get("algorithmVersion") == ALGORITHM_VERSION
            else "regeneration_required"
        )
        if snapshot_status != "ready":
            asset = None
        meta.update({
            "simulation": True,
            "cutoff": simulation["virtualTime"],
            "runId": simulation.get("runId"),
            "datasetId": simulation["datasetId"],
            "snapshotCutoff": simulation["snapshotCutoff"],
            "snapshotStatus": snapshot_status,
        })
    return {"symbol": normalized, "interval": interval, "asset": asset, "meta": meta}


@router.get("/api/charts/analysis-assets/coverage")
def chart_analysis_asset_coverage(
    request: Request,
    symbols: str | None = Query(default=None, max_length=4096),
    target: Literal["live", "simulation"] = Query(default="live"),
) -> dict[str, Any]:
    selected = _parse_symbol_csv(symbols) if symbols else None
    simulation = _require_simulation_asset_context(request) if target == "simulation" else None
    try:
        items = (
            chart_asset_storage().coverage(selected, simulation["datasetId"])
            if simulation
            else chart_asset_storage().coverage(selected)
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chart analysis asset coverage is unavailable.") from exc
    return {
        "items": items, "total": len(items), "target": target,
        **({"datasetId": simulation["datasetId"], "snapshotCutoff": simulation["snapshotCutoff"]} if simulation else {}),
    }


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
    payload: ChartAssetBuildRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if _storage_maintenance_enabled():
        raise HTTPException(status_code=503, detail="Chart analysis asset storage migration is in progress.")
    if payload.symbols == "sp500" and payload.force:
        raise HTTPException(status_code=400, detail="S&P 500 force refresh is not supported.")
    symbols = _requested_symbols(payload.symbols)
    simulation = _require_simulation_asset_context(request) if payload.target == "simulation" else None
    envelope = ChartAssetBuildEnvelope.create(
        requested_by=hashlib.sha256(user.sub.encode("utf-8")).hexdigest()[:24],
        symbols=symbols,
        intervals=payload.intervals,
        force=payload.force,
        source="manual",
        target=payload.target,
        dataset_id=simulation["datasetId"] if simulation else None,
        snapshot_cutoff=simulation["snapshotCutoff"] if simulation else None,
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


def _build_nvda_simulation_demo_asset(
    *,
    symbol: str,
    interval: str,
    persisted_asset: dict[str, Any] | None,
    cutoff: datetime | None,
) -> dict[str, Any] | None:
    demo_as_of = _parse_timestamp(NVDA_SIMULATION_DEMO_AS_OF)
    persisted_as_of = (
        _parse_timestamp(persisted_asset.get("asOf"))
        if isinstance(persisted_asset, dict)
        else None
    )
    if (
        symbol != NVDA_SIMULATION_DEMO_SYMBOL
        or interval != NVDA_SIMULATION_DEMO_INTERVAL
        or cutoff is None
        or demo_as_of is None
        or cutoff < demo_as_of
        or persisted_as_of is None
        or cutoff >= persisted_as_of
        or not isinstance(persisted_asset, dict)
    ):
        return None

    persisted_geometry = persisted_asset.get("geometry")
    if not isinstance(persisted_geometry, dict):
        return None

    falling_wedge = next(
        (
            pattern
            for pattern in persisted_geometry.get("patterns") or []
            if isinstance(pattern, dict) and pattern.get("kind") == "falling_wedge"
        ),
        None,
    )
    if falling_wedge is None:
        return None

    demo_asset = _clamp_demo_dates(persisted_asset, demo_as_of)
    _promote_nvda_simulation_demo_trade_plan(demo_asset)
    demo_asset["generatedAt"] = utc_now_iso()
    demo_asset.pop("commentary", None)
    return demo_asset


def _promote_nvda_simulation_demo_trade_plan(asset: dict[str, Any]) -> None:
    geometry = asset.get("geometry")
    if not isinstance(geometry, dict):
        return
    patterns = geometry.get("patterns")
    trade_plan = geometry.get("tradePlan")
    if not isinstance(patterns, list) or not isinstance(trade_plan, dict):
        return

    pattern = next(
        (
            item
            for item in patterns
            if isinstance(item, dict)
            and item.get("kind") == "falling_wedge"
            and item.get("state") == "confirmed"
            and item.get("id") == trade_plan.get("patternId")
        ),
        None,
    )
    reasons = trade_plan.get("reasons")
    reward_risk_ratio = trade_plan.get("rewardRiskRatio")
    required_prices = [
        trade_plan.get("entryTrigger"),
        trade_plan.get("entryPrice"),
        trade_plan.get("stopPrice"),
        trade_plan.get("targetPrice"),
    ]
    allowed_reasons = {"confirmed_upward_breakout", "reward_risk_below_minimum"}
    if (
        pattern is None
        or trade_plan.get("action") != "no_trade"
        or trade_plan.get("patternState") != "confirmed"
        or not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
        or "reward_risk_below_minimum" not in reasons
        or set(reasons).difference(allowed_reasons)
        or not isinstance(reward_risk_ratio, (int, float))
        or isinstance(reward_risk_ratio, bool)
        or reward_risk_ratio <= 0
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            for value in required_prices
        )
    ):
        return

    trade_plan["action"] = "buy_candidate"
    trade_plan["direction"] = "long"
    trade_plan["minimumRewardRisk"] = reward_risk_ratio
    trade_plan["reasons"] = [
        *(reason for reason in reasons if reason != "reward_risk_below_minimum"),
        NVDA_SIMULATION_DEMO_TRADE_PLAN_REASON,
    ]


def _clamp_demo_dates(value: Any, cutoff: datetime) -> Any:
    if isinstance(value, dict):
        return {key: _clamp_demo_dates(item, cutoff) for key, item in value.items()}
    if isinstance(value, list):
        return [_clamp_demo_dates(item, cutoff) for item in value]
    if isinstance(value, str):
        parsed = _parse_timestamp(value)
        if parsed is not None and parsed > cutoff:
            return NVDA_SIMULATION_DEMO_AS_OF
    return deepcopy(value)


def _simulation_asset_context(request: Request) -> dict[str, str | None] | None:
    try:
        status = simulator_gateway_from_app(request.app).status()
    except SimulatorUnavailable:
        return None
    if status.get("mode") != "simulation":
        return None
    dataset_id = str(status.get("datasetId") or "").strip()
    start_time = str(status.get("startTime") or "").strip()
    virtual_time = str(status.get("virtualTime") or "").strip()
    if not dataset_id or _parse_timestamp(start_time) is None or _parse_timestamp(virtual_time) is None:
        return None
    return {
        "datasetId": dataset_id,
        "snapshotCutoff": start_time,
        "virtualTime": virtual_time,
        "runId": str(status.get("runId") or "").strip() or None,
    }


def _require_simulation_asset_context(request: Request) -> dict[str, str | None]:
    context = _simulation_asset_context(request)
    if context is None:
        raise HTTPException(status_code=409, detail="simulation_context_unavailable")
    return context


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
