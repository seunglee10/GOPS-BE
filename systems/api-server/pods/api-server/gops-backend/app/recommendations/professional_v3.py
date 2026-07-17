from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.sectors import normalize_sector, sector_payload_fields

from .professional import close_returns, completed_daily, parse_datetime, position_value
from .professional_v2 import RISK_PRESETS, stable_digest
from .scoring import filter_candles_for_session, min_candle_count, normalize_symbol


ALGORITHM_VERSION = "deterministic-evidence-v3"
RULE_SET_VERSION = "deterministic-evidence-v3.1"
FACTOR_SCHEMA_VERSION = "recommendation-evidence-blocks.v3"
PREFERENCE_MODEL_VERSION = "continuous-preference-v3"
EVENT_SCHEMA_VERSION = "recommendation-preference-event.v3"
BLOCK_KEYS = (
    "trendStrength",
    "participationConfirmation",
    "priceStructure",
    "catalystQuality",
    "executionQuality",
    "qualityStability",
)
STYLE_BLOCK_WEIGHTS: dict[str, dict[str, float]] = {
    "momentum": dict(zip(BLOCK_KEYS, (35, 25, 20, 10, 10, 0), strict=True)),
    "balanced": dict(zip(BLOCK_KEYS, (25, 20, 15, 10, 15, 15), strict=True)),
    "stable": dict(zip(BLOCK_KEYS, (15, 10, 10, 5, 25, 35), strict=True)),
}
RISK_WEIGHTS = {"conservative": 0.35, "balanced": 0.25, "aggressive": 0.15}
SPREAD_CAPS_BPS = {"conservative": 25.0, "balanced": 40.0, "aggressive": 75.0}
RELIABILITY_MINIMUM = 70.0
MAX_SOFT_PENALTY = 30.0
FRESHNESS_SECONDS = {"regular": 120.0, "pre": 300.0}
# Real feeds may omit a small number of corrected/empty one-minute buckets. 380
# is the explicit lower bound for the documented "approximately 390" gate.
MIN_PREVIOUS_SESSION_CANDLES = 380

CONTINUOUS_FACTORS = (
    "currentSessionRelativeStrength",
    "last60MinuteRelativeStrength",
    "oneDayRelativeStrength",
    "fiveDayRelativeStrength",
    "high52WeekProximity",
    "clockAdjustedVolumeRatio",
    "abnormalDollarVolume",
    "closingLocationValue",
    "participationPersistence",
    "vwapHoldQuality",
    "medianDollarVolume",
    "quotedSpreadBps",
    "realizedVolatility",
    "downsideVolatility",
)
LOWER_IS_BETTER = {"quotedSpreadBps", "realizedVolatility", "downsideVolatility"}
OPTIONAL_FACTORS = (
    "catalystQuality",
    "valueQuality",
    "companyQuality",
    "growthQuality",
    "earningsRevisionQuality",
)


@dataclass(frozen=True)
class EvidenceContext:
    session_mode: str
    now: datetime
    market_items: list[dict[str, Any]]
    candles_by_symbol: dict[str, list[dict[str, Any]]]
    daily_candles_by_symbol: dict[str, list[dict[str, Any]]]
    previous_session_candles_by_symbol: dict[str, list[dict[str, Any]]]
    news_by_symbol: dict[str, list[dict[str, Any]]]
    fundamentals_by_symbol: dict[str, dict[str, Any]]
    fundamental_provenance: dict[str, Any]


@dataclass(frozen=True)
class EvidenceSnapshotResult:
    candidates: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    source_digests: dict[str, str]
    input_digest: str


@dataclass(frozen=True)
class EvidenceRankingResult:
    items: list[dict[str, Any]]
    candidate_features: list[dict[str, Any]]
    qualified_count: int
    rejected_by_reason: dict[str, int]


def rules_snapshot() -> dict[str, Any]:
    return {
        "ruleSetVersion": RULE_SET_VERSION,
        "factorSchemaVersion": FACTOR_SCHEMA_VERSION,
        "styleWeights": STYLE_BLOCK_WEIGHTS,
        "riskWeights": RISK_WEIGHTS,
        "spreadCapsBps": SPREAD_CAPS_BPS,
        "freshnessSeconds": FRESHNESS_SECONDS,
        "minimumPreviousSessionCandles": MIN_PREVIOUS_SESSION_CANDLES,
        "reliabilityMinimum": RELIABILITY_MINIMUM,
        "maxSoftPenalty": MAX_SOFT_PENALTY,
        "winsorization": [0.01, 0.99],
        "normalizationBlend": {"market": 0.60, "sector": 0.40},
        "blockWeights": {
            "trendStrength": {
                "currentSessionRelativeStrength": 0.35,
                "last60MinuteRelativeStrength": 0.25,
                "oneDayRelativeStrength": 0.15,
                "fiveDayRelativeStrength": 0.15,
                "high52WeekProximity": 0.10,
            },
            "participationConfirmation": {
                "clockAdjustedVolumeRatio": 0.40,
                "abnormalDollarVolume": 0.30,
                "closingLocationValue": 0.15,
                "participationPersistence": 0.15,
            },
            "priceStructure": {
                "confirmedBreakoutSupport": 0.40,
                "vwapHoldQuality": 0.30,
                "higherLowQuality": 0.20,
                "gapAcceptance": 0.10,
            },
            "executionQuality": {
                "medianDollarVolume": 0.50,
                "quotedSpreadBps": 0.30,
                "freshnessScore": 0.20,
            },
            "qualityStability": {
                "inverseVolatility": 0.30,
                "valueQuality": 0.25,
                "companyQuality": 0.25,
                "growthQuality": 0.10,
                "earningsRevisionQuality": 0.10,
            },
        },
        "riskPresets": RISK_PRESETS,
        "penaltyPolicy": {
            "overextensionAtrStart": 2.0,
            "overextensionMax": 10.0,
            "volatilityLimits": {"conservative": 0.035, "balanced": 0.055, "aggressive": 0.08},
            "volatilityMismatchMax": 8.0,
            "trendParticipationDisagreement": 35.0,
            "weakConfirmationPenalty": 6.0,
            "limitedPortfolioEvidencePenalty": 5.0,
            "concentrationProximityRatio": 0.90,
            "concentrationProximityPenalty": 5.0,
            "totalCap": MAX_SOFT_PENALTY,
        },
        "reliabilityWeights": {
            "coverage": 0.30,
            "freshness": 0.25,
            "sourceQuality": 0.20,
            "agreement": 0.15,
            "confirmation": 0.10,
        },
        "preference": {
            "maximumWeight": 0.15,
            "confidenceFloor": 0.20,
            "longHalfLifeDays": 60,
            "sessionHalfLifeDays": 3,
        },
        "portfolio": {
            "maximumTestWeightPct": 5.0,
            "returnWindowDays": 60,
            "covariance": {"sample": 0.70, "diagonal": 0.30},
        },
    }


def build_evidence_snapshot(context: EvidenceContext) -> EvidenceSnapshotResult:
    item_by_symbol = {
        normalize_symbol(item.get("symbol")): dict(item)
        for item in context.market_items
        if normalize_symbol(item.get("symbol")) not in {"", "SPY"}
    }
    spy_session = filter_candles_for_session(
        context.candles_by_symbol.get("SPY") or [], context.session_mode, context.now
    )
    spy_daily = completed_daily(context.daily_candles_by_symbol.get("SPY") or [], context.now)
    rejected: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    for symbol in sorted(item_by_symbol):
        item = item_by_symbol[symbol]
        reasons = base_rejection_reasons(symbol, item, context, spy_session, spy_daily)
        if reasons:
            rejected.append({"symbol": symbol, "rejectionReasons": reasons})
            continue
        raw = raw_factors(symbol, item, context, spy_session, spy_daily)
        if raw is None:
            rejected.append({"symbol": symbol, "rejectionReasons": ["invalid_required_market_values"]})
            continue
        raw_rows.append({
            "symbol": symbol,
            "sector": normalize_sector(item.get("sector")),
            "industry": str(item.get("industry") or "Unclassified"),
            "changePercent": _finite(item.get("changePercent")),
            "rawFactors": raw,
            "marketItem": _bounded_market_item(item),
        })
    normalized = normalize_cross_section(raw_rows)
    candidates: list[dict[str, Any]] = []
    for row in raw_rows:
        symbol = row["symbol"]
        factors = normalized[symbol]
        blocks = block_scores(factors, row["rawFactors"])
        reliability_components = evidence_reliability_components(row["rawFactors"], factors, blocks)
        reliability = weighted_sum(reliability_components, {
            "coverage": 0.30,
            "freshness": 0.25,
            "sourceQuality": 0.20,
            "agreement": 0.15,
            "confirmation": 0.10,
        })
        balanced_score = weighted_sum(blocks, {key: weight / 100.0 for key, weight in STYLE_BLOCK_WEIGHTS["balanced"].items()})
        raw = row["rawFactors"]
        payload = {
            **row,
            "normalizedFactors": factors,
            "blockScores": blocks,
            "availableBlocks": available_blocks(raw),
            "baseSetupScore": round(balanced_score, 4),
            "evidenceReliability": round(reliability, 4),
            "reliabilityComponents": reliability_components,
            "rejectionReasons": [],
            "dailyReturns60": raw.pop("_dailyReturns60", []),
            "inputDigest": stable_digest({
                "ruleSetVersion": RULE_SET_VERSION,
                "symbol": symbol,
                "cutoff": context.now,
                "raw": raw,
                "normalized": factors,
            }),
        }
        candidates.append(payload)
    for rejected_row in rejected:
        symbol = rejected_row["symbol"]
        item = item_by_symbol[symbol]
        candidates.append({
            "symbol": symbol,
            "sector": normalize_sector(item.get("sector")),
            "industry": str(item.get("industry") or "Unclassified"),
            "changePercent": _finite(item.get("changePercent")),
            "rawFactors": {},
            "marketItem": _bounded_market_item(item),
            "normalizedFactors": {},
            "blockScores": {},
            "availableBlocks": [],
            "baseSetupScore": 0.0,
            "evidenceReliability": 0.0,
            "reliabilityComponents": {},
            "rejectionReasons": list(rejected_row["rejectionReasons"]),
            "dailyReturns60": [],
            "inputDigest": stable_digest({
                "ruleSetVersion": RULE_SET_VERSION,
                "symbol": symbol,
                "cutoff": context.now,
                "rejectionReasons": rejected_row["rejectionReasons"],
            }),
        })
    candidates.sort(key=lambda row: row["symbol"])
    source_digests = {
        "market": stable_digest(context.market_items),
        "candles": stable_digest({key: value for key, value in sorted(context.candles_by_symbol.items())}),
        "daily": stable_digest({key: value for key, value in sorted(context.daily_candles_by_symbol.items())}),
        "previousSession": stable_digest({
            key: value for key, value in sorted(context.previous_session_candles_by_symbol.items())
        }),
        "news": stable_digest({key: value for key, value in sorted(context.news_by_symbol.items())}),
        "fundamentals": stable_digest({
            "rows": context.fundamentals_by_symbol,
            "provenance": context.fundamental_provenance,
        }),
    }
    input_digest = stable_digest({
        "ruleSetVersion": RULE_SET_VERSION,
        "cutoff": context.now,
        "sourceDigests": source_digests,
        "symbols": sorted(item_by_symbol),
    })
    return EvidenceSnapshotResult(candidates, rejected, source_digests, input_digest)


def base_rejection_reasons(
    symbol: str,
    item: dict[str, Any],
    context: EvidenceContext,
    spy_session: list[dict[str, Any]],
    spy_daily: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    if item.get("tradable") is False or item.get("active") is False:
        reasons.append("not_tradable")
    if item.get("halted") is True or str(item.get("tradingStatus") or "").lower() in {"halted", "suspended"}:
        reasons.append("halted")
    item_available_at = parse_datetime(
        item.get("availableAt") or item.get("priceUpdatedAt") or item.get("updatedAt")
    )
    if item_available_at is not None and item_available_at > context.now:
        reasons.append("future_market_snapshot")
    session = filter_candles_for_session(
        context.candles_by_symbol.get(symbol) or [], context.session_mode, context.now
    )
    daily = completed_daily(context.daily_candles_by_symbol.get(symbol) or [], context.now)
    previous_session = context.previous_session_candles_by_symbol.get(symbol) or []
    spy_previous_session = context.previous_session_candles_by_symbol.get("SPY") or []
    minimum = min_candle_count(context.session_mode, context.now)
    if len(session) < minimum:
        reasons.append("insufficient_session_candles")
    if len(spy_session) < minimum:
        reasons.append("insufficient_spy_session_candles")
    if len(previous_session) < MIN_PREVIOUS_SESSION_CANDLES:
        reasons.append("insufficient_previous_session_candles")
    if len(spy_previous_session) < MIN_PREVIOUS_SESSION_CANDLES:
        reasons.append("insufficient_spy_previous_session_candles")
    if len(daily) < 252:
        reasons.append("insufficient_daily_history")
    if len(spy_daily) < 252:
        reasons.append("insufficient_spy_daily_history")
    if any(_available_at(row) and _available_at(row) > context.now for row in daily):
        reasons.append("future_daily_input")
    if any(_available_at(row) and _available_at(row) > context.now for row in spy_daily):
        reasons.append("future_spy_daily_input")
    if any(_available_at(row) and _available_at(row) > context.now for row in previous_session):
        reasons.append("future_previous_session_input")
    if any(_available_at(row) and _available_at(row) > context.now for row in spy_previous_session):
        reasons.append("future_spy_previous_session_input")
    latest = _latest_timestamp(session)
    spy_latest = _latest_timestamp(spy_session)
    limit = FRESHNESS_SECONDS.get(context.session_mode, FRESHNESS_SECONDS["regular"])
    if latest is None or latest > context.now or (context.now - latest).total_seconds() > limit:
        reasons.append("stale_session_data")
    if spy_latest is None or spy_latest > context.now or (context.now - spy_latest).total_seconds() > limit:
        reasons.append("stale_spy_data")
    return reasons


def raw_factors(
    symbol: str,
    item: dict[str, Any],
    context: EvidenceContext,
    spy_session: list[dict[str, Any]],
    spy_daily: list[dict[str, Any]],
) -> dict[str, Any] | None:
    session = sorted(
        filter_candles_for_session(context.candles_by_symbol.get(symbol) or [], context.session_mode, context.now),
        key=_timestamp_text,
    )
    daily = completed_daily(context.daily_candles_by_symbol.get(symbol) or [], context.now)
    previous_session = sorted(context.previous_session_candles_by_symbol.get(symbol) or [], key=_timestamp_text)
    spy_previous_session = sorted(context.previous_session_candles_by_symbol.get("SPY") or [], key=_timestamp_text)
    if not session or not spy_session or len(daily) < 252 or len(spy_daily) < 252:
        return None
    latest = session[-1]
    close = _positive(latest.get("close"))
    opening = _positive(session[0].get("open") or session[0].get("close"))
    spy_close = _positive(spy_session[-1].get("close"))
    spy_open = _positive(spy_session[0].get("open") or spy_session[0].get("close"))
    if not all((close, opening, spy_close, spy_open)):
        return None
    daily_closes = [_positive(row.get("close")) for row in daily]
    spy_daily_closes = [_positive(row.get("close")) for row in spy_daily]
    if not all(daily_closes[-6:]) or not all(spy_daily_closes[-6:]):
        return None
    current_strength = _percent_return(opening, close) - _percent_return(spy_open, spy_close)
    last60_strength = _window_return(session, 60) - _window_return(spy_session, 60)
    one_day_strength = _percent_return(daily_closes[-2], daily_closes[-1]) - _percent_return(spy_daily_closes[-2], spy_daily_closes[-1])
    five_day_strength = _percent_return(daily_closes[-6], daily_closes[-1]) - _percent_return(spy_daily_closes[-6], spy_daily_closes[-1])
    dollar_volumes = [_positive(row.get("close")) * _positive(row.get("volume")) for row in daily[-21:-1]]
    median_adv = statistics.median([value for value in dollar_volumes if value > 0] or [0.0])
    elapsed = len(session)
    previous_elapsed_volume = sum(_positive(row.get("volume")) for row in previous_session[:elapsed])
    current_elapsed_volume = sum(_positive(row.get("volume")) for row in session)
    current_elapsed_dollar_volume = sum(
        _positive(row.get("close")) * _positive(row.get("volume")) for row in session
    )
    expected_session_minutes = 330 if context.session_mode == "pre" else 390
    elapsed_fraction = min(1.0, elapsed / expected_session_minutes)
    clock_volume_ratio = current_elapsed_volume / previous_elapsed_volume if previous_elapsed_volume > 0 else None
    recent_volume = sum(_positive(row.get("volume")) for row in session[-30:])
    earlier_volume = sum(_positive(row.get("volume")) for row in session[-60:-30])
    persistence = recent_volume / earlier_volume if earlier_volume > 0 else None
    high = max((_positive(row.get("high")) for row in session), default=0.0)
    low = min((_positive(row.get("low")) for row in session), default=0.0)
    clv = (2 * close - high - low) / (high - low) if high > low else 0.0
    high52 = max((_positive(row.get("high")) for row in daily[-252:]), default=0.0)
    atr = _atr(daily[-15:])
    vwap = _session_vwap(session)
    vwap_hold = (close - vwap) / atr if vwap and atr else None
    daily_returns = close_returns(daily[-61:])
    downside = [value for value in daily_returns if value < 0]
    latest_timestamp = _latest_timestamp(session)
    freshness_limit = FRESHNESS_SECONDS.get(context.session_mode, FRESHNESS_SECONDS["regular"])
    age_seconds = max(0.0, (context.now - latest_timestamp).total_seconds()) if latest_timestamp else freshness_limit
    freshness_score = _clamp(100.0 * (1.0 - age_seconds / freshness_limit))
    fundamentals = context.fundamentals_by_symbol.get(symbol) or {}
    news_score, news_available = catalyst_quality(context.news_by_symbol.get(symbol) or [], context.now)
    spread = _first_finite(item, "quotedSpreadBps", "spreadBps", "bidAskSpreadBps")
    source_quality = market_source_quality(item, session, daily, context.fundamental_provenance)
    structure = price_structure_metrics(session, daily, vwap, atr)
    last60 = session[-60:]
    quote_bid = _first_finite(item, "bid", "bidPrice", "bestBid")
    quote_ask = _first_finite(item, "ask", "askPrice", "bestAsk")
    return {
        "currentSessionRelativeStrength": current_strength,
        "last60MinuteRelativeStrength": last60_strength,
        "oneDayRelativeStrength": one_day_strength,
        "fiveDayRelativeStrength": five_day_strength,
        "high52WeekProximity": close / high52 if high52 else None,
        "clockAdjustedVolumeRatio": clock_volume_ratio,
        "abnormalDollarVolume": math.log(
            max(current_elapsed_dollar_volume, 1.0)
            / max(median_adv * elapsed_fraction, 1.0)
        ),
        "closingLocationValue": clv,
        "participationPersistence": persistence,
        "confirmedBreakoutSupport": structure["confirmedBreakoutSupport"],
        "vwapHoldQuality": vwap_hold,
        "higherLowQuality": structure["higherLowQuality"],
        "gapAcceptance": structure["gapAcceptance"],
        "catalystQuality": news_score,
        "catalystAvailable": news_available,
        "medianDollarVolume": median_adv,
        "quotedSpreadBps": spread,
        "freshnessScore": freshness_score,
        "realizedVolatility": statistics.pstdev(daily_returns) if len(daily_returns) >= 20 else None,
        "downsideVolatility": statistics.pstdev(downside) if len(downside) >= 5 else None,
        "valueQuality": _finite(fundamentals.get("value")),
        "companyQuality": _finite(fundamentals.get("quality")),
        "growthQuality": _finite(fundamentals.get("growth")),
        "earningsRevisionQuality": _finite(fundamentals.get("earningsRevision")),
        "fundamentalAvailable": bool(fundamentals),
        "sourceQuality": source_quality,
        "latestClose": close,
        "sessionOpen": opening,
        "sessionHigh": high,
        "sessionLow": low,
        "last60MinuteLow": min((_positive(row.get("low")) for row in last60), default=0.0),
        "vwap": vwap,
        "atr": atr,
        "quoteBid": quote_bid,
        "quoteAsk": quote_ask,
        "tickSize": 0.01,
        "overextensionAtr": abs(close - vwap) / atr if vwap and atr else None,
        "_dailyReturns60": daily_returns,
    }


def normalize_cross_section(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result = {row["symbol"]: {} for row in rows}
    sectors: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sectors.setdefault(row["sector"], []).append(row)
    for key in CONTINUOUS_FACTORS:
        market = average_rank_percentiles(rows, key, inverse=key in LOWER_IS_BETTER)
        sector_scores: dict[str, float] = {}
        for sector_rows in sectors.values():
            current = average_rank_percentiles(sector_rows, key, inverse=key in LOWER_IS_BETTER)
            sector_scores.update(current)
        for row in rows:
            symbol = row["symbol"]
            raw = row["rawFactors"].get(key)
            if _finite(raw) is None:
                result[symbol][key] = 50.0
            else:
                result[symbol][key] = round(0.60 * market[symbol] + 0.40 * sector_scores.get(symbol, 50.0), 4)
    for row in rows:
        raw = row["rawFactors"]
        symbol = row["symbol"]
        for key in (
            "confirmedBreakoutSupport", "higherLowQuality", "gapAcceptance", "catalystQuality",
            "freshnessScore", "valueQuality", "companyQuality", "growthQuality", "earningsRevisionQuality",
        ):
            result[symbol][key] = round(_clamp(_finite(raw.get(key)) if _finite(raw.get(key)) is not None else 50.0), 4)
    return result


def average_rank_percentiles(rows: list[dict[str, Any]], key: str, *, inverse: bool = False) -> dict[str, float]:
    values = [(row["symbol"], _finite(row["rawFactors"].get(key))) for row in rows]
    available = [(symbol, value) for symbol, value in values if value is not None]
    if not available:
        return {symbol: 50.0 for symbol, _value in values}
    raw_values = sorted(value for _symbol, value in available)
    lower = _quantile(raw_values, 0.01)
    upper = _quantile(raw_values, 0.99)
    winsorized = sorted((min(upper, max(lower, value)), symbol) for symbol, value in available)
    by_value: dict[float, list[tuple[int, str]]] = {}
    for index, (value, symbol) in enumerate(winsorized):
        by_value.setdefault(value, []).append((index, symbol))
    denominator = max(1, len(winsorized) - 1)
    result: dict[str, float] = {}
    for group in by_value.values():
        average_index = statistics.mean(index for index, _symbol in group)
        score = average_index / denominator * 100.0 if len(winsorized) > 1 else 50.0
        if inverse:
            score = 100.0 - score
        for _index, symbol in group:
            result[symbol] = score
    for symbol, value in values:
        if value is None:
            result[symbol] = 50.0
    return result


def block_scores(factors: dict[str, float], raw: dict[str, Any]) -> dict[str, float]:
    trend = weighted_sum(factors, {
        "currentSessionRelativeStrength": 0.35,
        "last60MinuteRelativeStrength": 0.25,
        "oneDayRelativeStrength": 0.15,
        "fiveDayRelativeStrength": 0.15,
        "high52WeekProximity": 0.10,
    })
    participation = weighted_sum(factors, {
        "clockAdjustedVolumeRatio": 0.40,
        "abnormalDollarVolume": 0.30,
        "closingLocationValue": 0.15,
        "participationPersistence": 0.15,
    })
    structure = weighted_sum(factors, {
        "confirmedBreakoutSupport": 0.40,
        "vwapHoldQuality": 0.30,
        "higherLowQuality": 0.20,
        "gapAcceptance": 0.10,
    })
    execution = weighted_sum(factors, {
        "medianDollarVolume": 0.50,
        "quotedSpreadBps": 0.30,
        "freshnessScore": 0.20,
    })
    inverse_volatility = statistics.mean([factors["realizedVolatility"], factors["downsideVolatility"]])
    quality_components: list[tuple[float, float]] = []
    if not raw or _finite(raw.get("realizedVolatility")) is not None or _finite(raw.get("downsideVolatility")) is not None:
        quality_components.append((inverse_volatility, 0.30))
    for key, weight in (
        ("valueQuality", 0.25),
        ("companyQuality", 0.25),
        ("growthQuality", 0.10),
        ("earningsRevisionQuality", 0.10),
    ):
        if not raw or _optional_available(raw, key):
            quality_components.append((factors[key], weight))
    quality_weight = sum(weight for _value, weight in quality_components)
    quality = (
        sum(value * weight for value, weight in quality_components) / quality_weight
        if quality_weight > 0
        else 50.0
    )
    return {
        "trendStrength": round(_clamp(trend), 4),
        "participationConfirmation": round(_clamp(participation), 4),
        "priceStructure": round(_clamp(structure), 4),
        "catalystQuality": round(_clamp(factors["catalystQuality"]), 4),
        "executionQuality": round(_clamp(execution), 4),
        "qualityStability": round(_clamp(quality), 4),
    }


def available_blocks(raw: dict[str, Any]) -> list[str]:
    """Return only evidence blocks backed by observed inputs.

    Missing optional catalyst/fundamental inputs are omitted from scoring rather
    than represented as a neutral 50-point opinion. Quality remains available
    when observed volatility data exists.
    """
    blocks = ["trendStrength", "participationConfirmation", "priceStructure", "executionQuality"]
    if raw.get("catalystAvailable") is True:
        blocks.append("catalystQuality")
    if (
        _finite(raw.get("realizedVolatility")) is not None
        or _finite(raw.get("downsideVolatility")) is not None
        or any(_optional_available(raw, key) for key in OPTIONAL_FACTORS if key != "catalystQuality")
    ):
        blocks.append("qualityStability")
    return [key for key in BLOCK_KEYS if key in blocks]


def evidence_reliability_components(
    raw: dict[str, Any], factors: dict[str, float], blocks: dict[str, float]
) -> dict[str, float]:
    observed = [key for key in CONTINUOUS_FACTORS if _finite(raw.get(key)) is not None]
    optional = [key for key in OPTIONAL_FACTORS if _optional_available(raw, key)]
    coverage = (len(observed) + len(optional)) / (len(CONTINUOUS_FACTORS) + len(OPTIONAL_FACTORS)) * 100.0
    trend_direction = blocks["trendStrength"] >= 50
    participation_direction = blocks["participationConfirmation"] >= 50
    structure_direction = blocks["priceStructure"] >= 50
    catalyst_direction = blocks["catalystQuality"] >= 50
    comparisons = [
        trend_direction == participation_direction,
        trend_direction == structure_direction,
    ]
    if raw.get("catalystAvailable"):
        comparisons.append(trend_direction == catalyst_direction)
    agreement = sum(comparisons) / len(comparisons) * 100.0
    # Reliability describes whether the setup is supported by independent
    # measurements, not whether those measurements are bullish. A weak setup
    # with complete candles/volume/quote evidence can be scored low with high
    # confidence; counting only blocks above 60 conflates signal strength with
    # data reliability and makes confidence look like a success probability.
    confirmation_groups = (
        ("currentSessionRelativeStrength", "last60MinuteRelativeStrength"),
        ("clockAdjustedVolumeRatio", "abnormalDollarVolume"),
        ("confirmedBreakoutSupport", "vwapHoldQuality"),
        ("medianDollarVolume", "quotedSpreadBps"),
    )
    confirmation = (
        sum(all(_finite(raw.get(key)) is not None for key in group) for group in confirmation_groups)
        / len(confirmation_groups)
        * 100.0
    )
    return {
        "coverage": round(_clamp(coverage), 4),
        "freshness": round(_clamp(float(raw.get("freshnessScore") or 0.0)), 4),
        "sourceQuality": round(_clamp(float(raw.get("sourceQuality") or 0.0)), 4),
        "agreement": round(_clamp(agreement), 4),
        "confirmation": round(_clamp(confirmation), 4),
    }


def rank_evidence_candidates(
    candidates: list[dict[str, Any]],
    *,
    profile: Any,
    preference_state: dict[str, Any],
    risk_state: dict[str, Any],
    watchlist_symbols: list[str],
    portfolio_positions: list[dict[str, Any]],
    portfolio_snapshot: dict[str, Any] | None,
    position_daily_candles: dict[str, list[dict[str, Any]]],
    active_symbol: str | None,
    now: datetime,
    snapshot_id: int | None,
    penalize_missing_portfolio: bool = True,
    exclude_portfolio_hard_caps: bool = True,
) -> EvidenceRankingResult:
    excluded = {normalize_symbol(value) for value in watchlist_symbols}
    excluded.update(normalize_symbol(row.get("symbol")) for row in portfolio_positions)
    excluded.update(normalize_symbol(value) for value in profile.excluded_symbols)
    if active_symbol:
        excluded.add(normalize_symbol(active_symbol))
    excluded_sectors = {normalize_sector(value) for value in profile.excluded_sectors}
    rejection_counts: dict[str, int] = {}
    eligible: list[dict[str, Any]] = []
    preset = RISK_PRESETS.get(profile.risk_level, RISK_PRESETS["balanced"])
    spread_cap = SPREAD_CAPS_BPS.get(profile.risk_level, SPREAD_CAPS_BPS["balanced"])
    for row in candidates:
        reasons: list[str] = list(row.get("rejectionReasons") or [])
        if reasons:
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        if row["symbol"] in excluded:
            reasons.append("user_symbol_exclusion")
        if normalize_sector(row.get("sector")) in excluded_sectors:
            reasons.append("user_sector_exclusion")
        raw = row.get("rawFactors") or {}
        if float(raw.get("medianDollarVolume") or 0.0) < preset["minimumMedianDollarVolume"]:
            reasons.append("minimum_liquidity")
        spread = _finite(raw.get("quotedSpreadBps"))
        if spread is None:
            reasons.append("missing_quoted_spread")
        elif spread > spread_cap:
            reasons.append("spread_cap")
        if float(row.get("evidenceReliability") or 0.0) < RELIABILITY_MINIMUM:
            reasons.append("evidence_reliability")
        trial = standardized_test_weight(row, portfolio_snapshot, profile.risk_level, now)
        if exclude_portfolio_hard_caps and trial is not None and trial <= 0:
            reasons.append("portfolio_hard_cap")
        if reasons:
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        eligible.append(row)
    block_means = {
        key: statistics.mean([float(row["blockScores"][key]) for row in eligible])
        for key in BLOCK_KEYS
    } if eligible else {key: 50.0 for key in BLOCK_KEYS}
    preference_weights = _number_map(preference_state.get("effectiveWeights"))
    preference_confidence = float(preference_state.get("preferenceConfidence") or 0.2)
    preference_weight = 0.15 * _clamp((preference_confidence - 0.20) / 0.80, 0.0, 1.0)
    portfolio_reliable = _portfolio_snapshot_fresh(portfolio_snapshot, now)
    risk_weight = RISK_WEIGHTS.get(profile.risk_level, RISK_WEIGHTS["balanced"]) if portfolio_reliable else 0.0
    output: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    for row in eligible:
        blocks = row["blockScores"]
        available = [key for key in row.get("availableBlocks") or BLOCK_KEYS if key in blocks]
        configured_style_weights = STYLE_BLOCK_WEIGHTS.get(
            profile.recommendation_style, STYLE_BLOCK_WEIGHTS["balanced"]
        )
        available_weight = sum(float(configured_style_weights[key]) for key in available) or 1.0
        style_weights = {
            key: (float(configured_style_weights[key]) / available_weight * 100.0 if key in available else 0.0)
            for key in BLOCK_KEYS
        }
        contributions = {
            key: round(float(blocks[key]) * float(style_weights[key]) / 100.0, 4)
            for key in BLOCK_KEYS
        }
        base = round(sum(contributions.values()), 4)
        portfolio = portfolio_compatibility(
            row,
            portfolio_snapshot,
            position_daily_candles,
            now=now,
            risk_level=profile.risk_level,
        )
        penalties, warnings = soft_penalties(
            row,
            portfolio=portfolio,
            portfolio_reliable=portfolio_reliable,
            risk_level=profile.risk_level,
            penalize_missing_portfolio=penalize_missing_portfolio,
        )
        missing_optional = [key for key in OPTIONAL_FACTORS if not _optional_available(row.get("rawFactors") or {}, key)]
        spread = _finite((row.get("rawFactors") or {}).get("quotedSpreadBps"))
        if spread is not None and spread >= 0.80 * spread_cap:
            warnings.append("호가 스프레드가 현재 위험성향의 실행 한도에 근접합니다.")
        median_dollar_volume = float((row.get("rawFactors") or {}).get("medianDollarVolume") or 0.0)
        if median_dollar_volume < 1.25 * preset["minimumMedianDollarVolume"]:
            warnings.append("중앙값 거래대금이 현재 위험성향의 유동성 하한에 근접합니다.")
        adjusted = round(_clamp(base - sum(penalties.values())), 4)
        active_preference = {
            key: preference_weights.get(key, 0.0) if key in available else 0.0
            for key in BLOCK_KEYS
        }
        preference_total = sum(active_preference.values()) or 1.0
        preference_fit = sum(float(blocks[key]) * active_preference[key] / preference_total for key in BLOCK_KEYS)
        adjusted_contribution = round(
            (1.0 - preference_weight - risk_weight) * adjusted, 8
        )
        preference_contribution = round(preference_weight * preference_fit, 8)
        portfolio_contribution = round(risk_weight * portfolio["score"], 8)
        final = round(
            adjusted_contribution + preference_contribution + portfolio_contribution,
            4,
        )
        reasons = evidence_reasons(contributions, blocks)
        metrics = {
            "algorithmVersion": ALGORITHM_VERSION,
            "ruleSetVersion": RULE_SET_VERSION,
            "factorSchemaVersion": FACTOR_SCHEMA_VERSION,
            "cutoff": row.get("evaluatedAt"),
            "evidenceSnapshotId": snapshot_id,
            "sourceDigests": row.get("sourceDigests") or {},
            "inputDigest": row.get("inputDigest"),
            "rawFactors": row.get("rawFactors") or {},
            "normalizedFactors": row.get("normalizedFactors") or {},
            "blockScores": blocks,
            "blockContributions": contributions,
            "availableBlocks": available,
            "effectiveStyleWeights": {key: round(value, 4) for key, value in style_weights.items() if value > 0},
            "baseSetupScore": round(base, 4),
            "softPenalties": penalties,
            "adjustedSetupScore": round(adjusted, 4),
            "evidenceReliability": round(float(row.get("evidenceReliability") or 0.0), 4),
            "missingOptionalFactors": missing_optional,
            "stale": False,
            "reliabilityComponents": row.get("reliabilityComponents") or {},
            "preferenceFitScore": round(preference_fit, 4),
            "preferenceConfidence": round(preference_confidence, 8),
            "preferenceWeight": round(preference_weight, 8),
            "adjustedSetupContribution": adjusted_contribution,
            "preferenceContribution": preference_contribution,
            "portfolioCompatibility": portfolio,
            "portfolioWeight": round(risk_weight, 8),
            "portfolioContribution": portfolio_contribution,
            "personalScore": final,
            "confidenceMeaning": "evidence_reliability_not_success_probability",
            "changePercent": row.get("changePercent"),
        }
        output.append({
            "symbol": row["symbol"],
            "action": "buy",
            "rank": 0,
            "score": final,
            "confidence": round(float(row.get("evidenceReliability") or 0.0) / 100.0, 4),
            "changePercent": row.get("changePercent"),
            **sector_payload_fields(row.get("sector")),
            "reasons": reasons,
            "riskWarnings": warnings,
            "metricsSnapshot": metrics,
        })
        features.append({
            "symbol": row["symbol"],
            "evaluated_at": row.get("evaluatedAt") or now,
            "market_factor_scores": row.get("normalizedFactors") or {},
            "fundamental_factor_scores": {
                key: value for key, value in (row.get("normalizedFactors") or {}).items()
                if key in {"valueQuality", "companyQuality", "growthQuality", "earningsRevisionQuality"}
            },
            "available_factor_scores": blocks,
            "candidate_mean_scores": block_means,
            "base_alpha_score": base,
            "fundamental_score": blocks.get("qualityStability"),
            "fundamental_weight": 0.0,
            "fundamental_status": "ready" if (row.get("rawFactors") or {}).get("fundamentalAvailable") else "missing",
            "feature_snapshot_id": str(snapshot_id) if snapshot_id is not None else None,
            "feature_schema_version": FACTOR_SCHEMA_VERSION,
            "feature_version": RULE_SET_VERSION,
            "input_digest": row.get("inputDigest"),
        })
    output.sort(key=lambda item: (-float(item["score"]), str(item["symbol"])))
    selected = output[:15]
    for index, item in enumerate(selected, start=1):
        item["rank"] = index
    return EvidenceRankingResult(selected, features, len(eligible), rejection_counts)


def process_evidence_preference_events(
    previous: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    *,
    style: str,
    cutoff: datetime,
    existing_order_strengths: dict[str, float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = _preference_state(previous, style, cutoff)
    events: list[dict[str, Any]] = []
    strengths = dict(existing_order_strengths or {})
    for fill in sorted(fills, key=lambda row: (_aware_datetime(row.get("decision_at"), cutoff), int(row.get("id") or 0))):
        event_time = min(cutoff, _aware_datetime(fill.get("decision_at"), cutoff))
        _decay_preference_state(state, event_time)
        event = _preference_event(fill)
        if str(fill.get("side") or "").lower() != "buy":
            event.update(event_status="skipped", skip_reason="sell_excluded")
            events.append(event)
            continue
        scores = _to_block_scores(_number_map(fill.get("feature_scores")))
        means = _to_block_scores(_number_map(fill.get("candidate_mean_scores")))
        if not scores or not means:
            event.update(event_status="skipped", skip_reason="missing_point_in_time_feature")
            events.append(event)
            continue
        quantity = _positive(fill.get("incremental_filled_qty") or fill.get("cumulative_filled_qty"))
        price = _positive(fill.get("average_fill_price"))
        if quantity <= 0 or price <= 0:
            event.update(event_status="skipped", skip_reason="invalid_fill_notional")
            events.append(event)
            continue
        notional = quantity * price
        equity = _positive(fill.get("portfolio_equity"))
        raw_strength = max(0.25, min(1.0, (notional / equity) / 0.05)) if equity else 0.25
        order_id = str(fill.get("order_id") or "")
        remaining = max(0.0, 1.0 - strengths.get(order_id, 0.0))
        strength = min(raw_strength, remaining)
        if strength <= 0:
            event.update(event_status="skipped", skip_reason="order_strength_cap_reached")
            events.append(event)
            continue
        relative = {
            key: _clamp((scores[key] - means[key]) / 50.0, -1.0, 1.0)
            for key in BLOCK_KEYS if key in scores and key in means
        }
        if not relative:
            event.update(event_status="skipped", skip_reason="missing_point_in_time_feature")
            events.append(event)
            continue
        for key, exposure in relative.items():
            delta = 0.20 * strength * exposure
            state["longTermLogits"][key] = _clamp(state["longTermLogits"].get(key, 0.0) + delta, -2.0, 2.0)
            state["sessionLogits"][key] = _clamp(state["sessionLogits"].get(key, 0.0) + delta, -2.0, 2.0)
        state["longSampleCount"] += strength
        state["sessionSampleCount"] += strength
        strengths[order_id] = strengths.get(order_id, 0.0) + strength
        event.update(
            event_status="applied",
            skip_reason=None,
            relative_exposure=relative,
            event_strength=strength,
            incremental_notional=notional,
            portfolio_equity=equity or None,
            order_cumulative_strength=strengths[order_id],
        )
        events.append(event)
    _decay_preference_state(state, cutoff)
    return _finalize_preference_state(state, cutoff), events


def standardized_test_weight(
    item: dict[str, Any], snapshot: dict[str, Any] | None, risk_level: str, now: datetime
) -> float | None:
    if not _portfolio_snapshot_fresh(snapshot, now):
        return None
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    equity = _portfolio_equity(payload)
    if equity <= 0:
        return None
    preset = RISK_PRESETS.get(risk_level, RISK_PRESETS["balanced"])
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    symbol = item["symbol"]
    sector = normalize_sector(item.get("sector"))
    symbol_pct = sum(position_value(row) for row in positions if normalize_symbol(row.get("symbol")) == symbol) / equity * 100.0
    sector_pct = sum(position_value(row) for row in positions if normalize_sector(row.get("sector")) == sector) / equity * 100.0
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cash") or account.get("cashForeign"))
    cash_pct = cash / equity * 100.0 if equity else 0.0
    return max(0.0, min(
        5.0,
        cash_pct,
        preset["maxSingleStockPct"] - symbol_pct,
        preset["maxSectorPct"] - sector_pct,
    ))


def portfolio_compatibility(
    item: dict[str, Any],
    snapshot: dict[str, Any] | None,
    position_daily_candles: dict[str, list[dict[str, Any]]],
    *,
    now: datetime,
    risk_level: str,
) -> dict[str, Any]:
    if not _portfolio_snapshot_fresh(snapshot, now):
        return {"score": 50.0, "status": "unavailable", "testWeightPct": None, "components": {}}
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    equity = _portfolio_equity(payload)
    test_weight = standardized_test_weight(item, snapshot, risk_level, now)
    if equity <= 0 or test_weight is None:
        return {"score": 50.0, "status": "unavailable", "testWeightPct": None, "components": {}}
    preset = RISK_PRESETS.get(risk_level, RISK_PRESETS["balanced"])
    sector = normalize_sector(item.get("sector"))
    sector_value = sum(position_value(row) for row in positions if normalize_sector(row.get("sector")) == sector)
    sector_after = sector_value / equity * 100.0 + test_weight
    sector_score = _clamp(100.0 * (1.0 - sector_after / max(preset["maxSectorPct"], 1.0)))
    portfolio_assets = _portfolio_asset_returns(
        positions, position_daily_candles, now=now, equity=equity
    )
    candidate_returns = [float(value) for value in item.get("dailyReturns60") or []]
    length = min(
        [len(candidate_returns), *[len(series) for series, _weight in portfolio_assets]],
        default=0,
    )
    if length >= 20:
        aligned_assets = [(series[-length:], weight) for series, weight in portfolio_assets]
        candidate_returns = candidate_returns[-length:]
        portfolio_returns = [
            sum(series[index] * weight for series, weight in aligned_assets)
            for index in range(length)
        ]
        correlation = _correlation(portfolio_returns, candidate_returns)
        correlation_score = _clamp((1.0 - correlation) * 50.0)
        delta = test_weight / 100.0
        current_variance = _shrunk_portfolio_variance(
            [series for series, _weight in aligned_assets],
            [weight for _series, weight in aligned_assets],
        )
        new_variance = _shrunk_portfolio_variance(
            [*[series for series, _weight in aligned_assets], candidate_returns],
            [*[weight for _series, weight in aligned_assets], delta],
        )
        marginal_change = new_variance - current_variance
        marginal_score = _clamp(50.0 - marginal_change * 100_000.0)
    else:
        correlation = 0.0
        correlation_score = 50.0
        marginal_change = 0.0
        marginal_score = 50.0
    raw = item.get("rawFactors") or {}
    liquidity_score = _clamp((math.log10(max(float(raw.get("medianDollarVolume") or 1.0), 1.0)) - 5.0) / 4.0 * 100.0)
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cash") or account.get("cashForeign"))
    cash_score = _clamp((cash / equity if equity else 0.0) / 0.15 * 100.0)
    liquidity_cash = (liquidity_score + cash_score) / 2.0
    components = {
        "sectorDiversification": sector_score,
        "correlationBenefit": correlation_score,
        "marginalVariance": marginal_score,
        "liquidityCashCompatibility": liquidity_cash,
    }
    score = weighted_sum(components, {
        "sectorDiversification": 0.30,
        "correlationBenefit": 0.30,
        "marginalVariance": 0.25,
        "liquidityCashCompatibility": 0.15,
    })
    return {
        "score": round(score, 4),
        "status": "ready",
        "testWeightPct": round(test_weight, 4),
        "sectorAfterPct": round(sector_after, 4),
        "historicalCorrelation": round(correlation, 6),
        "marginalVarianceChange": round(marginal_change, 10),
        "components": {key: round(value, 4) for key, value in components.items()},
        "covarianceMethod": "0.70_sample_plus_0.30_diagonal",
    }


def soft_penalties(
    item: dict[str, Any],
    *,
    portfolio: dict[str, Any],
    portfolio_reliable: bool,
    risk_level: str,
    penalize_missing_portfolio: bool = True,
) -> tuple[dict[str, float], list[str]]:
    raw = item.get("rawFactors") or {}
    blocks = item.get("blockScores") or {}
    penalties: dict[str, float] = {}
    warnings: list[str] = []
    extension = _finite(raw.get("overextensionAtr"))
    if extension is not None and extension > 2.0:
        penalties["overextension"] = min(10.0, (extension - 2.0) * 4.0)
        warnings.append("현재 가격이 VWAP 대비 ATR 기준으로 과도하게 이격되어 있습니다.")
    realized = _finite(raw.get("realizedVolatility"))
    volatility_limit = {"conservative": 0.035, "balanced": 0.055, "aggressive": 0.08}.get(risk_level, 0.055)
    if realized is not None and realized > volatility_limit:
        penalties["volatilityMismatch"] = min(8.0, (realized - volatility_limit) * 200.0)
        warnings.append("최근 변동성이 현재 위험성향의 안정성 기준보다 높습니다.")
    if abs(float(blocks.get("trendStrength") or 50.0) - float(blocks.get("participationConfirmation") or 50.0)) >= 35:
        penalties["weakConfirmation"] = 6.0
        warnings.append("가격 강도와 거래 참여 확인이 서로 일치하지 않습니다.")
    if not portfolio_reliable and penalize_missing_portfolio:
        penalties["limitedPortfolioEvidence"] = 5.0
        warnings.append("신뢰 가능한 최신 포트폴리오 근거가 없어 포트폴리오 적합도를 반영하지 않았습니다.")
    elif portfolio_reliable and float(portfolio.get("sectorAfterPct") or 0.0) >= 0.9 * RISK_PRESETS.get(risk_level, RISK_PRESETS["balanced"])["maxSectorPct"]:
        penalties["concentrationProximity"] = 5.0
        warnings.append("표준 시험 비중을 추가하면 섹터 집중 한도에 근접합니다.")
    total = sum(penalties.values())
    if total > MAX_SOFT_PENALTY:
        scale = MAX_SOFT_PENALTY / total
        penalties = {key: round(value * scale, 4) for key, value in penalties.items()}
    else:
        penalties = {key: round(value, 4) for key, value in penalties.items()}
    return penalties, warnings


def evidence_reasons(contributions: dict[str, float], blocks: dict[str, float]) -> list[dict[str, Any]]:
    labels = {
        "trendStrength": "현재 및 최근 구간의 시장 대비 가격 강도",
        "participationConfirmation": "거래량과 거래대금의 참여 확인",
        "priceStructure": "VWAP·돌파·지지 구조",
        "catalystQuality": "관련성·방향·신선도를 반영한 촉매 품질",
        "executionQuality": "유동성·스프레드·데이터 신선도",
        "qualityStability": "기업 품질과 변동성 안정성",
    }
    ranked = sorted(contributions, key=lambda key: (contributions[key], blocks[key], key), reverse=True)
    return [
        {
            "type": "evidence_block",
            "text": f"{labels[key]} 점수가 {blocks[key]:.1f}/100입니다.",
            "weight": round(contributions[key], 4),
        }
        for key in ranked[:5] if contributions[key] > 0
    ]


def catalyst_quality(rows: list[dict[str, Any]], now: datetime) -> tuple[float, bool]:
    scored: list[tuple[float, float]] = []
    seen: set[str] = set()
    for row in rows:
        identity = str(row.get("articleId") or row.get("id") or row.get("url") or row.get("headline") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        observed = parse_datetime(
            row.get("availableAt") or row.get("receivedAt") or row.get("received_at")
            or row.get("localizedAt") or row.get("publishedAt") or row.get("published_at")
        )
        if observed is None or observed > now:
            continue
        age_hours = max(0.0, (now - observed).total_seconds() / 3600.0)
        decay = math.exp(-age_hours / 48.0)
        direction_text = str(row.get("impactDirection") or row.get("sentiment") or "").lower()
        direction = 1.0 if direction_text in {"positive", "bullish", "up"} else -1.0 if direction_text in {"negative", "bearish", "down"} else 0.0
        relevance_value = _first_present(row, "relevanceScoreV2", "relevance", "relevanceScore")
        if relevance_value is None:
            continue
        relevance = _unit_score(relevance_value, default=0.0)
        if relevance <= 0:
            continue
        novelty = _unit_score(_first_present(row, "novelty", "noveltyScore"), default=0.5)
        source = _unit_score(_first_present(row, "sourceQuality"), default=0.75 if row.get("url") else 0.5)
        scored.append((direction * relevance * novelty * source * decay, relevance * decay))
    if not scored:
        return 50.0, False
    signed = sum(score for score, _weight in scored) / len(scored)
    return _clamp(50.0 + 50.0 * signed), True


def price_structure_metrics(
    session: list[dict[str, Any]], daily: list[dict[str, Any]], vwap: float, atr: float
) -> dict[str, float]:
    close = _positive(session[-1].get("close"))
    previous_high = max((_positive(row.get("high")) for row in daily[-20:]), default=0.0)
    breakout = 100.0 if previous_high and close > previous_high and all(_positive(row.get("close")) > previous_high for row in session[-2:]) else 70.0 if vwap and close >= vwap else 30.0
    chunks = [session[max(0, len(session) - size):] for size in (60, 40, 20)]
    lows = [min((_positive(row.get("low")) for row in chunk), default=0.0) for chunk in chunks]
    higher_low = 100.0 if len(lows) == 3 and lows[0] <= lows[1] <= lows[2] else 50.0
    daily_open = _positive(session[0].get("open") or session[0].get("close"))
    previous_close = _positive(daily[-1].get("close"))
    gap = _percent_return(previous_close, daily_open) if previous_close else 0.0
    acceptance = 100.0 if gap > 0 and close >= daily_open else 20.0 if gap < 0 and close < daily_open else 50.0
    return {
        "confirmedBreakoutSupport": breakout,
        "higherLowQuality": higher_low,
        "gapAcceptance": acceptance,
    }


def market_source_quality(
    item: dict[str, Any], session: list[dict[str, Any]], daily: list[dict[str, Any]], provenance: dict[str, Any]
) -> float:
    explicit_sources = [
        str(item.get("priceSource") or "").lower(),
        *[str(row.get("sourceClass") or row.get("source_class") or "").lower() for row in session[-5:]],
        *[str(row.get("sourceClass") or row.get("source_class") or "").lower() for row in daily[-5:]],
    ]
    explicit_sources = [source for source in explicit_sources if source]
    if any(source in {"synthetic", "simulation", "fallback"} for source in explicit_sources):
        return 0.0
    base = 100.0 if explicit_sources and all("canonical" in source or "clickhouse" in source or "redis" in source or "alpaca" in source for source in explicit_sources) else 80.0
    if provenance.get("status") not in {None, "ready"}:
        base -= 10.0
    return _clamp(base)


def weighted_sum(values: dict[str, float], weights: dict[str, float]) -> float:
    return sum(float(values.get(key, 0.0)) * weight for key, weight in weights.items())


def _preference_state(previous: dict[str, Any] | None, style: str, cutoff: datetime) -> dict[str, Any]:
    prior = dict(STYLE_BLOCK_WEIGHTS.get(style, STYLE_BLOCK_WEIGHTS["balanced"]))
    previous = previous or {}
    long_logits = _to_block_logits(_number_map(previous.get("longTermLogits")))
    session_logits = _to_block_logits(_number_map(previous.get("sessionLogits")))
    return {
        "priorStyle": style if style in STYLE_BLOCK_WEIGHTS else "balanced",
        "priorWeights": prior,
        "longTermLogits": {key: long_logits.get(key, 0.0) for key in BLOCK_KEYS},
        "sessionLogits": {key: session_logits.get(key, 0.0) for key in BLOCK_KEYS},
        "longSampleCount": float(previous.get("longSampleCount") or 0.0),
        "sessionSampleCount": float(previous.get("sessionSampleCount") or 0.0),
        "asOf": _aware_datetime(previous.get("asOf"), cutoff),
    }


def _finalize_preference_state(state: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    n_session = max(0.0, float(state.get("sessionSampleCount") or 0.0))
    gamma = min(0.5, n_session / (n_session + 3.0))
    logits = {
        key: math.log(max(float(state["priorWeights"].get(key, 0.1)), 0.1) / 100.0)
        + float(state["longTermLogits"].get(key, 0.0))
        + gamma * float(state["sessionLogits"].get(key, 0.0))
        for key in BLOCK_KEYS
    }
    largest = max(logits.values(), default=0.0)
    exponentials = {key: math.exp(value - largest) for key, value in logits.items()}
    total = sum(exponentials.values()) or 1.0
    effective = {key: value / total * 100.0 for key, value in exponentials.items()}
    n_long = max(0.0, float(state.get("longSampleCount") or 0.0))
    confidence = (5.0 + n_long) / (25.0 + n_long)
    result = {
        **state,
        "gamma": gamma,
        "effectiveWeights": effective,
        "preferenceConfidence": confidence,
        "asOf": cutoff,
        "preferenceModelVersion": PREFERENCE_MODEL_VERSION,
        "factorSchemaVersion": FACTOR_SCHEMA_VERSION,
    }
    result["inputDigest"] = stable_digest(result)
    return result


def _decay_preference_state(state: dict[str, Any], target: datetime) -> None:
    previous = _aware_datetime(state.get("asOf"), target)
    if target <= previous:
        return
    days = (target - previous).total_seconds() / 86_400.0
    for key in BLOCK_KEYS:
        state["longTermLogits"][key] *= 0.5 ** (days / 60.0)
        state["sessionLogits"][key] *= 0.5 ** (days / 3.0)
    state["longSampleCount"] *= 0.5 ** (days / 60.0)
    state["sessionSampleCount"] *= 0.5 ** (days / 3.0)
    state["asOf"] = target


def _preference_event(fill: dict[str, Any]) -> dict[str, Any]:
    return {
        "fill_history_id": fill.get("id"),
        "user_sub": fill.get("user_sub"),
        "order_id": fill.get("order_id"),
        "symbol": fill.get("symbol"),
        "side": str(fill.get("side") or "").lower(),
        "decision_at": fill.get("decision_at"),
        "candidate_run_id": fill.get("candidate_run_id"),
        "candidate_feature_id": fill.get("candidate_feature_id"),
        "evidence_candidate_id": fill.get("evidence_candidate_id"),
        "event_status": "skipped",
        "skip_reason": None,
        "relative_exposure": {},
        "event_strength": 0.0,
        "incremental_notional": None,
        "portfolio_equity": None,
        "order_cumulative_strength": 0.0,
        "provenance": {"source": "evidence_candidate"},
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "processing_version": PREFERENCE_MODEL_VERSION,
    }


def _to_block_scores(values: dict[str, float]) -> dict[str, float]:
    if all(key in values for key in BLOCK_KEYS):
        return {key: values[key] for key in BLOCK_KEYS}
    mapping = {
        "trendStrength": ("oneDayRelativeStrength", "previousSessionStrength", "lastHourRelativeStrength", "high52WeekProximity"),
        "participationConfirmation": ("abnormalDollarVolume", "closingLocationValue"),
        "priceStructure": (),
        "catalystQuality": ("newsImpact",),
        "executionQuality": ("liquidityQuality",),
        "qualityStability": ("lowVolatilityQuality",),
    }
    result = {}
    for block, keys in mapping.items():
        available = [values[key] for key in keys if key in values]
        result[block] = statistics.mean(available) if available else 50.0
    return result if any(key in values for keys in mapping.values() for key in keys) else {}


def _to_block_logits(values: dict[str, float]) -> dict[str, float]:
    if all(key in values for key in BLOCK_KEYS):
        return {key: _clamp(values[key], -2.0, 2.0) for key in BLOCK_KEYS}
    mapping = {
        "trendStrength": ("oneDayRelativeStrength", "previousSessionStrength", "lastHourRelativeStrength", "high52WeekProximity"),
        "participationConfirmation": ("abnormalDollarVolume", "closingLocationValue"),
        "priceStructure": (),
        "catalystQuality": ("newsImpact",),
        "executionQuality": ("liquidityQuality",),
        "qualityStability": ("lowVolatilityQuality",),
    }
    result: dict[str, float] = {}
    for block, keys in mapping.items():
        available = [values[key] for key in keys if key in values]
        result[block] = _clamp(statistics.mean(available), -2.0, 2.0) if available else 0.0
    return result if any(key in values for keys in mapping.values() for key in keys) else {}


def _portfolio_asset_returns(
    positions: list[dict[str, Any]],
    daily_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    now: datetime,
    equity: float,
) -> list[tuple[list[float], float]]:
    assets: list[tuple[list[float], float]] = []
    for position in positions:
        symbol = normalize_symbol(position.get("symbol"))
        returns = close_returns(completed_daily(daily_by_symbol.get(symbol) or [], now)[-61:])
        value = position_value(position)
        if len(returns) >= 20 and value > 0 and equity > 0:
            assets.append((returns, value / equity))
    return assets


def _shrunk_portfolio_variance(series: list[list[float]], weights: list[float]) -> float:
    if not series or len(series) != len(weights):
        return 0.0
    variance = 0.0
    for left_index, left in enumerate(series):
        for right_index, right in enumerate(series):
            sample = _covariance(left, right)
            shrunk = sample if left_index == right_index else 0.70 * sample
            variance += weights[left_index] * weights[right_index] * shrunk
    return variance


def _portfolio_snapshot_fresh(snapshot: dict[str, Any] | None, now: datetime) -> bool:
    if not snapshot:
        return False
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    observed = parse_datetime(snapshot.get("source_as_of") or payload.get("sourceAsOf") or payload.get("asOf"))
    basis = str(payload.get("valuationBasis") or payload.get("valuation_basis") or "market_value").lower()
    return bool(observed and observed <= now and now - observed <= timedelta(hours=24) and basis != "cost_basis")


def _portfolio_equity(payload: dict[str, Any]) -> float:
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    explicit = _positive(payload.get("totalValue") or payload.get("totalEvaluationAmount") or account.get("totalValueForeign") or account.get("totalValue"))
    if explicit:
        return explicit
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cashForeign") or account.get("cash"))
    return sum(position_value(row) for row in positions) + cash


def _bounded_market_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "symbol", "sector", "industry", "changePercent", "lastPrice", "sessionDollarVolume",
        "quotedSpreadBps", "spreadBps", "bidAskSpreadBps", "bid", "ask", "bidPrice", "askPrice",
        "priceSource", "priceUpdatedAt",
        "tradable", "active", "halted", "tradingStatus",
    )
    return {key: item.get(key) for key in allowed if key in item}


def _session_vwap(rows: list[dict[str, Any]]) -> float:
    explicit = _finite(rows[-1].get("vwap")) if rows else None
    if explicit is not None and explicit > 0:
        return explicit
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        volume = _positive(row.get("volume"))
        typical = statistics.mean([_positive(row.get("high")), _positive(row.get("low")), _positive(row.get("close"))])
        if volume > 0 and typical > 0:
            numerator += typical * volume
            denominator += volume
    return numerator / denominator if denominator else 0.0


def _atr(rows: list[dict[str, Any]]) -> float:
    values = []
    previous_close = None
    for row in rows:
        high, low, close = _positive(row.get("high")), _positive(row.get("low")), _positive(row.get("close"))
        if high and low:
            values.append(max(high - low, abs(high - previous_close) if previous_close else 0.0, abs(low - previous_close) if previous_close else 0.0))
        previous_close = close or previous_close
    return statistics.mean(values) if values else 0.0


def _window_return(rows: list[dict[str, Any]], length: int) -> float:
    ordered = sorted(rows, key=_timestamp_text)[-length:]
    if len(ordered) < 2:
        return 0.0
    return _percent_return(_positive(ordered[0].get("open") or ordered[0].get("close")), _positive(ordered[-1].get("close")))


def _percent_return(start: float, end: float) -> float:
    return (end / start - 1.0) * 100.0 if start > 0 else 0.0


def _correlation(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum((value - statistics.mean(left)) ** 2 for value in left) * sum((value - statistics.mean(right)) ** 2 for value in right))
    return sum((a - statistics.mean(left)) * (b - statistics.mean(right)) for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0


def _covariance(left: list[float], right: list[float]) -> float:
    if len(left) < 2:
        return 0.0
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    return sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)) / len(left)


def _quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _optional_available(raw: dict[str, Any], key: str) -> bool:
    if key == "catalystQuality":
        return bool(raw.get("catalystAvailable"))
    return _finite(raw.get(key)) is not None


def _unit_score(value: Any, *, default: float) -> float:
    parsed = _finite(value)
    if parsed is None:
        return default
    return _clamp(parsed if 0.0 <= parsed <= 1.0 else parsed / 100.0, 0.0, 1.0)


def _first_finite(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _finite(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _first_present(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _latest_timestamp(rows: list[dict[str, Any]]) -> datetime | None:
    values = [parse_datetime(row.get("timestamp") or row.get("eventTime") or row.get("updatedAt")) for row in rows]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _available_at(row: dict[str, Any]) -> datetime | None:
    return parse_datetime(
        row.get("availableAt") or row.get("available_at") or row.get("receivedAt")
        or row.get("received_at") or row.get("timestamp") or row.get("eventTime")
    )


def _timestamp_text(row: dict[str, Any]) -> str:
    return str(row.get("timestamp") or row.get("eventTime") or row.get("updatedAt") or "")


def _aware_datetime(value: Any, default: datetime) -> datetime:
    return parse_datetime(value) or default


def _number_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): parsed for key, child in value.items() if (parsed := _finite(child)) is not None}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive(value: Any) -> float:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed > 0 else 0.0


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, float(value)))
