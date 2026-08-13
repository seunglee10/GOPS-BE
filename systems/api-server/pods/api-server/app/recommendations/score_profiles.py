from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any


SCORE_PROFILE_SCHEMA_VERSION = "recommendation-score-profile.v1"
MAX_CUSTOM_SCORE_PROFILES = 20
SCORE_WEIGHT_TOLERANCE = 0.01
DEFAULT_RECOMMENDATION_STYLE = "stable"

BLOCK_KEYS = (
    "trendStrength",
    "participationConfirmation",
    "priceStructure",
    "catalystQuality",
    "executionQuality",
    "qualityStability",
)

SYSTEM_BLOCK_WEIGHTS: dict[str, dict[str, float]] = {
    "momentum": dict(zip(BLOCK_KEYS, (35, 25, 20, 10, 10, 0), strict=True)),
    "balanced": dict(zip(BLOCK_KEYS, (25, 20, 15, 10, 15, 15), strict=True)),
    "stable": dict(zip(BLOCK_KEYS, (15, 10, 10, 5, 25, 35), strict=True)),
}

DEFAULT_FACTOR_WEIGHTS: dict[str, dict[str, float]] = {
    "trendStrength": {
        "currentSessionRelativeStrength": 35,
        "last60MinuteRelativeStrength": 25,
        "oneDayRelativeStrength": 15,
        "fiveDayRelativeStrength": 15,
        "high52WeekProximity": 10,
    },
    "participationConfirmation": {
        "clockAdjustedVolumeRatio": 40,
        "abnormalDollarVolume": 30,
        "closingLocationValue": 15,
        "participationPersistence": 15,
    },
    "priceStructure": {
        "confirmedBreakoutSupport": 40,
        "vwapHoldQuality": 30,
        "higherLowQuality": 20,
        "gapAcceptance": 10,
    },
    "catalystQuality": {"catalystQuality": 100},
    "executionQuality": {
        "medianDollarVolume": 50,
        "quotedSpreadBps": 30,
        "freshnessScore": 20,
    },
    "qualityStability": {
        "realizedVolatility": 15,
        "downsideVolatility": 15,
        "valueQuality": 25,
        "companyQuality": 25,
        "growthQuality": 10,
        "earningsRevisionQuality": 10,
    },
}

DEFAULT_PORTFOLIO_FACTOR_WEIGHTS = {
    "sectorDiversification": 30,
    "correlationBenefit": 30,
    "marginalVariance": 25,
    "liquidityCashCompatibility": 15,
}

RISK_PORTFOLIO_WEIGHTS = {
    "conservative": 35,
    "balanced": 25,
    "aggressive": 15,
}


class ScoreProfileValidationError(ValueError):
    pass


def system_score_profile(style: str, risk_level: str = "balanced") -> dict[str, Any]:
    normalized_style = style if style in SYSTEM_BLOCK_WEIGHTS else DEFAULT_RECOMMENDATION_STYLE
    return {
        "type": "preset",
        "id": None,
        "name": {"momentum": "모멘텀", "balanced": "균형", "stable": "안정"}[normalized_style],
        "presetStyle": normalized_style,
        "revision": 1,
        "schemaVersion": SCORE_PROFILE_SCHEMA_VERSION,
        "blockWeights": deepcopy(SYSTEM_BLOCK_WEIGHTS[normalized_style]),
        "factorWeights": deepcopy(DEFAULT_FACTOR_WEIGHTS),
        "portfolioWeight": float(RISK_PORTFOLIO_WEIGHTS.get(risk_level, 25)),
        "portfolioFactorWeights": deepcopy(DEFAULT_PORTFOLIO_FACTOR_WEIGHTS),
    }


def normalize_score_profile_payload(value: dict[str, Any]) -> dict[str, Any]:
    block_weights = _validate_weight_group(value.get("blockWeights"), BLOCK_KEYS, "blockWeights")
    factor_source = value.get("factorWeights")
    if not isinstance(factor_source, dict) or set(factor_source) != set(BLOCK_KEYS):
        raise ScoreProfileValidationError("factorWeights must contain every supported block")
    factor_weights = {
        block: _validate_weight_group(
            factor_source.get(block),
            tuple(DEFAULT_FACTOR_WEIGHTS[block]),
            f"factorWeights.{block}",
        )
        for block in BLOCK_KEYS
    }
    portfolio_weight = _finite_weight(value.get("portfolioWeight"), "portfolioWeight")
    portfolio_factors = _validate_weight_group(
        value.get("portfolioFactorWeights"),
        tuple(DEFAULT_PORTFOLIO_FACTOR_WEIGHTS),
        "portfolioFactorWeights",
    )
    return {
        "schemaVersion": SCORE_PROFILE_SCHEMA_VERSION,
        "blockWeights": block_weights,
        "factorWeights": factor_weights,
        "portfolioWeight": portfolio_weight,
        "portfolioFactorWeights": portfolio_factors,
    }


def score_profile_digest(profile: dict[str, Any]) -> str:
    payload = {
        "schemaVersion": profile.get("schemaVersion") or SCORE_PROFILE_SCHEMA_VERSION,
        "blockWeights": profile.get("blockWeights") or {},
        "factorWeights": profile.get("factorWeights") or {},
        "portfolioWeight": profile.get("portfolioWeight"),
        "portfolioFactorWeights": profile.get("portfolioFactorWeights") or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def public_score_profile(row: dict[str, Any]) -> dict[str, Any]:
    profile = {
        "type": row.get("type") or "custom",
        "id": row.get("id"),
        "name": row.get("name"),
        "presetStyle": row.get("presetStyle"),
        "revision": int(row.get("revision") or 1),
        "schemaVersion": row.get("schema_version") or row.get("schemaVersion") or SCORE_PROFILE_SCHEMA_VERSION,
        "blockWeights": deepcopy(row.get("block_weights") or row.get("blockWeights") or {}),
        "factorWeights": deepcopy(row.get("factor_weights") or row.get("factorWeights") or {}),
        "portfolioWeight": float(row.get("portfolio_weight") if row.get("portfolio_weight") is not None else row.get("portfolioWeight") or 0),
        "portfolioFactorWeights": deepcopy(
            row.get("portfolio_factor_weights") or row.get("portfolioFactorWeights") or {}
        ),
        "createdAt": row.get("created_at") or row.get("createdAt"),
        "updatedAt": row.get("updated_at") or row.get("updatedAt"),
    }
    profile["digest"] = score_profile_digest(profile)
    return profile


def _validate_weight_group(value: Any, keys: tuple[str, ...], label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ScoreProfileValidationError(f"{label} must contain exactly: {', '.join(keys)}")
    normalized = {key: _finite_weight(value[key], f"{label}.{key}") for key in keys}
    if abs(sum(normalized.values()) - 100.0) > SCORE_WEIGHT_TOLERANCE:
        raise ScoreProfileValidationError(f"{label} weights must sum to 100")
    return normalized


def _finite_weight(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ScoreProfileValidationError(f"{label} must be a number between 0 and 100")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoreProfileValidationError(f"{label} must be a number between 0 and 100") from exc
    if not math.isfinite(number) or number < 0 or number > 100:
        raise ScoreProfileValidationError(f"{label} must be a number between 0 and 100")
    if abs(number - round(number, 2)) > 1e-9:
        raise ScoreProfileValidationError(f"{label} must have at most two decimal places")
    return round(number, 2)
