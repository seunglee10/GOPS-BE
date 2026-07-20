from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


NVDA_SIMULATION_DEMO_FALLBACK_AS_OF = "2026-07-14T04:00:00.000Z"
NVDA_SIMULATION_DEMO_DATASET_ID = "sp500-full-20260715-kst-v3"
NVDA_SIMULATION_DEMO_SYMBOL = "NVDA"
NVDA_SIMULATION_DEMO_INTERVAL = "1D"
NVDA_SIMULATION_DEMO_TRADE_PLAN_REASON = "simulation_demo_reward_risk_override"
NVDA_SIMULATION_DEMO_COMMENTARY_PROMPT_VERSION = "chart-commentary.ko.v5"


def is_nvda_simulation_demo_target(dataset_id: str | None, symbol: str, interval: str) -> bool:
    return (
        str(dataset_id or "").strip() == NVDA_SIMULATION_DEMO_DATASET_ID
        and str(symbol or "").strip().upper() == NVDA_SIMULATION_DEMO_SYMBOL
        and str(interval or "").strip() == NVDA_SIMULATION_DEMO_INTERVAL
    )


def project_nvda_simulation_demo_snapshot(
    *,
    dataset_id: str,
    base_asset: dict[str, Any],
    source_asset: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return one cutoff-bound NVDA demo asset before commentary generation.

    ``base_asset`` owns the persisted identity, canonical coverage and indicator
    snapshot. Only the existing, explicitly-scoped demo geometry is projected
    from the LIVE source. This keeps the manual SIM build atomic while avoiding
    any runtime Geometry or LLM work.
    """
    symbol = str(base_asset.get("symbol") or "").strip().upper()
    interval = str(base_asset.get("interval") or "").strip()
    if not is_nvda_simulation_demo_target(dataset_id, symbol, interval):
        return deepcopy(base_asset)
    if not isinstance(source_asset, dict):
        raise ValueError("NVDA simulation demo geometry source is unavailable")
    if (
        source_asset.get("assetVersion") != base_asset.get("assetVersion")
        or source_asset.get("algorithmVersion") != base_asset.get("algorithmVersion")
        or str(source_asset.get("symbol") or "").strip().upper() != symbol
        or str(source_asset.get("interval") or "").strip() != interval
    ):
        raise ValueError("NVDA simulation demo geometry source identity is invalid")

    source_geometry = source_asset.get("geometry")
    if not isinstance(source_geometry, dict) or not _confirmed_falling_wedge(source_geometry):
        raise ValueError("NVDA simulation demo falling-wedge geometry is unavailable")
    as_of = _required_timestamp_text(base_asset.get("asOf"), "NVDA simulation demo asset asOf")
    projected = deepcopy(base_asset)
    projected["geometry"] = clamp_demo_dates(source_geometry, as_of)
    projected.pop("commentary", None)
    promote_nvda_simulation_demo_trade_plan(projected)
    _validate_projected_demo_geometry(projected)
    return projected


def is_complete_nvda_simulation_demo_snapshot(asset: Any) -> bool:
    if not isinstance(asset, dict):
        return False
    if not is_nvda_simulation_demo_target(
        NVDA_SIMULATION_DEMO_DATASET_ID,
        str(asset.get("symbol") or ""),
        str(asset.get("interval") or ""),
    ):
        return False
    geometry = asset.get("geometry")
    commentary = asset.get("commentary")
    if not isinstance(geometry, dict) or not _confirmed_falling_wedge(geometry):
        return False
    if not isinstance(commentary, dict) or commentary.get("status") != "ready":
        return False
    if (
        commentary.get("version") != "chart-commentary.v2"
        or commentary.get("promptVersion") != NVDA_SIMULATION_DEMO_COMMENTARY_PROMPT_VERSION
    ):
        return False
    source_identity = commentary.get("sourceIdentity")
    if not isinstance(source_identity, dict):
        return False
    as_of = str(asset.get("asOf") or "")
    if (
        source_identity.get("geometryInputDigest") != asset.get("inputDigest")
        or source_identity.get("candlesAsOf") != as_of
        or source_identity.get("indicatorsAsOf") != as_of
        or not source_identity.get("contextDigest")
    ):
        return False
    groups = geometry.get("drawingGroups")
    if not isinstance(groups, dict) or any(not groups.get(group) for group in ("levels", "trend", "pattern")):
        return False
    trade_plan = geometry.get("tradePlan")
    if (
        not isinstance(trade_plan, dict)
        or trade_plan.get("action") != "buy_candidate"
        or trade_plan.get("direction") != "long"
        or NVDA_SIMULATION_DEMO_TRADE_PLAN_REASON not in (trade_plan.get("reasons") or [])
    ):
        return False
    drawing_ids = {
        str(item.get("id"))
        for item in geometry.get("drawings") or []
        if isinstance(item, dict) and item.get("id")
    }
    for reference in commentary.get("references") or []:
        if (
            isinstance(reference, dict)
            and reference.get("type") == "drawing"
            and not set(reference.get("drawingIds") or []).issubset(drawing_ids)
        ):
            return False
    try:
        as_of_time = _parse_timestamp(as_of)
    except ValueError:
        return False
    return not _contains_timestamp_after(geometry, as_of_time)


def clamp_demo_dates(value: Any, cutoff_text: str) -> Any:
    cutoff = _parse_timestamp(cutoff_text)
    if isinstance(value, dict):
        return {key: clamp_demo_dates(item, cutoff_text) for key, item in value.items()}
    if isinstance(value, list):
        return [clamp_demo_dates(item, cutoff_text) for item in value]
    if isinstance(value, str):
        try:
            parsed = _parse_timestamp(value)
        except ValueError:
            return value
        if parsed > cutoff:
            return cutoff_text
    return deepcopy(value)


def promote_nvda_simulation_demo_trade_plan(asset: dict[str, Any]) -> None:
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
        or not _positive_number(reward_risk_ratio)
        or not all(_positive_number(value) for value in required_prices)
    ):
        return
    trade_plan["action"] = "buy_candidate"
    trade_plan["direction"] = "long"
    trade_plan["minimumRewardRisk"] = reward_risk_ratio
    trade_plan["reasons"] = [
        *(reason for reason in reasons if reason != "reward_risk_below_minimum"),
        NVDA_SIMULATION_DEMO_TRADE_PLAN_REASON,
    ]


def _validate_projected_demo_geometry(asset: dict[str, Any]) -> None:
    geometry = asset.get("geometry")
    if not isinstance(geometry, dict) or not _confirmed_falling_wedge(geometry):
        raise ValueError("NVDA simulation demo projection lost its falling wedge")
    groups = geometry.get("drawingGroups")
    if not isinstance(groups, dict) or any(not groups.get(group) for group in ("levels", "trend", "pattern")):
        raise ValueError("NVDA simulation demo projection requires level, trend and pattern drawings")
    trace = geometry.get("analysisTrace")
    completeness = trace.get("completeness") if isinstance(trace, dict) else None
    if (
        not isinstance(trace, dict)
        or trace.get("version") != "geometry-analysis-trace-v2"
        or not isinstance(completeness, dict)
        or completeness.get("complete") is not True
        or completeness.get("detected") != completeness.get("stored")
    ):
        raise ValueError("NVDA simulation demo projection requires a complete analysis trace")
    trade_plan = geometry.get("tradePlan")
    if (
        not isinstance(trade_plan, dict)
        or trade_plan.get("action") != "buy_candidate"
        or trade_plan.get("direction") != "long"
        or NVDA_SIMULATION_DEMO_TRADE_PLAN_REASON not in (trade_plan.get("reasons") or [])
    ):
        raise ValueError("NVDA simulation demo projection requires its buy-only proposal")
    as_of = _parse_timestamp(str(asset.get("asOf") or ""))
    if _contains_timestamp_after(geometry, as_of):
        raise ValueError("NVDA simulation demo projection contains a future timestamp")


def _confirmed_falling_wedge(geometry: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("kind") == "falling_wedge"
        and item.get("state") == "confirmed"
        for item in geometry.get("patterns") or []
    )


def _contains_timestamp_after(value: Any, cutoff: datetime) -> bool:
    if isinstance(value, dict):
        return any(_contains_timestamp_after(item, cutoff) for item in value.values())
    if isinstance(value, list):
        return any(_contains_timestamp_after(item, cutoff) for item in value)
    if not isinstance(value, str):
        return False
    try:
        return _parse_timestamp(value) > cutoff
    except ValueError:
        return False


def _required_timestamp_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        _parse_timestamp(text)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    return text


def _parse_timestamp(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("timestamp is empty")
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
