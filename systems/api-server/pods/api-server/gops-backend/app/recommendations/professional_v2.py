from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .professional import parse_datetime


FUNDAMENTAL_FACTOR_KEYS = ("value", "quality", "growth", "earningsRevision")
FUNDAMENTAL_COMPONENT_WEIGHTS = {
    "value": 0.30,
    "quality": 0.35,
    "growth": 0.20,
    "earningsRevision": 0.15,
}
RISK_PRESETS: dict[str, dict[str, float]] = {
    "conservative": {
        "maxSingleStockPct": 15.0,
        "maxSectorPct": 30.0,
        "maxDailyLossPct": 2.0,
        "targetAnnualVolatilityPct": 20.0,
        "maxDrawdownPct": 6.0,
        "minimumMedianDollarVolume": 10_000_000.0,
        "maximumTurnoverPct": 25.0,
        "minimumCashPct": 15.0,
    },
    "balanced": {
        "maxSingleStockPct": 20.0,
        "maxSectorPct": 40.0,
        "maxDailyLossPct": 3.0,
        "targetAnnualVolatilityPct": 30.0,
        "maxDrawdownPct": 10.0,
        "minimumMedianDollarVolume": 5_000_000.0,
        "maximumTurnoverPct": 40.0,
        "minimumCashPct": 10.0,
    },
    "aggressive": {
        "maxSingleStockPct": 30.0,
        "maxSectorPct": 50.0,
        "maxDailyLossPct": 5.0,
        "targetAnnualVolatilityPct": 45.0,
        "maxDrawdownPct": 20.0,
        "minimumMedianDollarVolume": 1_000_000.0,
        "maximumTurnoverPct": 70.0,
        "minimumCashPct": 5.0,
    },
}


class FundamentalSnapshotProvider(Protocol):
    def snapshots_as_of(self, symbols: list[str], cutoff: datetime) -> Any:
        ...


@dataclass(frozen=True)
class FundamentalBatch:
    snapshots: dict[str, dict[str, Any]]
    snapshot_id: str | None = None
    schema_version: str | None = None
    feature_version: str | None = None
    digest: str | None = None
    source_as_of: datetime | None = None
    warnings: tuple[str, ...] = ()

def resolve_algorithm_version(env_value: str | None, *, enabled: bool, shadow: bool) -> tuple[str, bool]:
    explicit = str(env_value or "").strip().lower()
    if explicit:
        if explicit not in {"legacy", "professional-v1", "deterministic-evidence-v3"}:
            raise ValueError(
                "RECOMMENDATION_ALGORITHM_VERSION must be legacy, professional-v1, "
                "or deterministic-evidence-v3"
            )
        return explicit, False if explicit == "deterministic-evidence-v3" else shadow
    return ("professional-v1" if enabled else "legacy"), shadow

def normalize_fundamental_batch(payload: Any, symbols: list[str], cutoff: datetime) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if payload is None:
        return {}, {"status": "unavailable"}
    try:
        batch = _coerce_fundamental_batch(payload)
    except Exception as exc:
        return {}, {"status": "provider_error", "warning": exc.__class__.__name__}
    provenance = {
        "status": "ready",
        "snapshotId": batch.snapshot_id,
        "schemaVersion": batch.schema_version,
        "featureVersion": batch.feature_version,
        "digest": batch.digest,
        "sourceAsOf": batch.source_as_of.isoformat() if batch.source_as_of else None,
        "warnings": list(batch.warnings),
    }
    if batch.source_as_of and batch.source_as_of > cutoff:
        return {}, {**provenance, "status": "future_data"}
    if batch.snapshots and not all((batch.snapshot_id, batch.schema_version, batch.feature_version, batch.digest, batch.source_as_of)):
        return {}, {**provenance, "status": "invalid_provenance"}
    normalized: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        row = batch.snapshots.get(symbol.upper())
        if not isinstance(row, dict):
            continue
        row_as_of = parse_datetime(row.get("sourceAsOf") or row.get("source_as_of")) or batch.source_as_of
        scores = {key: _finite(row.get(key)) for key in FUNDAMENTAL_FACTOR_KEYS}
        quality = {key: _finite(row.get(key)) for key in ("coverage", "freshness", "sourceQuality")}
        if row_as_of and row_as_of > cutoff:
            normalized[symbol] = {"status": "future_data", "weight": 0.0, "scores": {}}
            continue
        if any(value is None or value < 0 or value > 100 for value in scores.values()):
            normalized[symbol] = {"status": "invalid", "weight": 0.0, "scores": {}}
            continue
        if any(value is None or value < 0 or value > 1 for value in quality.values()):
            normalized[symbol] = {"status": "invalid", "weight": 0.0, "scores": {}}
            continue
        fundamental_score = sum(scores[key] * FUNDAMENTAL_COMPONENT_WEIGHTS[key] for key in FUNDAMENTAL_FACTOR_KEYS)  # type: ignore[operator]
        weight = min(0.15, 0.15 * quality["coverage"] * quality["freshness"] * quality["sourceQuality"])  # type: ignore[operator]
        normalized[symbol] = {
            "status": "ready" if weight > 0 else "unavailable",
            "weight": weight,
            "score": fundamental_score,
            "scores": scores,
            "sourceAsOf": row_as_of,
        }
    return normalized, provenance

def stable_digest(value: Any, *, omit: set[str] | None = None) -> str:
    omit = omit or set()
    material = {key: child for key, child in value.items() if key not in omit} if isinstance(value, dict) else value
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _coerce_fundamental_batch(payload: Any) -> FundamentalBatch:
    if isinstance(payload, FundamentalBatch):
        return payload
    if payload is None:
        return FundamentalBatch({})
    if not isinstance(payload, dict):
        raise ValueError("fundamental batch must be an object")
    snapshots = payload.get("snapshots") or payload.get("symbols") or {}
    if not isinstance(snapshots, dict):
        raise ValueError("fundamental snapshots must be a symbol map")
    source_as_of = parse_datetime(payload.get("sourceAsOf") or payload.get("source_as_of"))
    return FundamentalBatch(
        snapshots={str(key).upper(): value for key, value in snapshots.items() if isinstance(value, dict)},
        snapshot_id=str(payload.get("snapshotId") or payload.get("snapshot_id") or "") or None,
        schema_version=str(payload.get("schemaVersion") or payload.get("schema_version") or "") or None,
        feature_version=str(payload.get("featureVersion") or payload.get("feature_version") or "") or None,
        digest=str(payload.get("digest") or "") or None,
        source_as_of=source_as_of,
        warnings=tuple(str(value) for value in payload.get("warnings", []) if value),
    )

def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
