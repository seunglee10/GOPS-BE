from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.core.sectors import normalize_sector

from .professional import (
    FACTOR_KEYS as MARKET_FACTOR_KEYS,
    RISK_BLEND,
    STYLE_WEIGHTS,
    ProfessionalContext,
    cross_sectional_scores,
    median_dollar_volume,
    parse_datetime,
    portfolio_fit_score,
    position_value,
    predicted_excess_return,
    raw_factors,
    weighted_score,
)


ALGORITHM_VERSION = "continuous-personalization-v2"
PREFERENCE_MODEL_VERSION = "continuous-preference-v2"
RISK_POLICY_VERSION = "continuous-risk-v2"
FACTOR_SCHEMA_VERSION = "recommendation-factors.v2"
FEATURE_VERSION = "continuous-candidate-features-v2"
EVENT_SCHEMA_VERSION = "recommendation-preference-event.v2"
FUNDAMENTAL_FACTOR_KEYS = ("value", "quality", "growth", "earningsRevision")
FACTOR_KEYS = (*MARKET_FACTOR_KEYS, *FUNDAMENTAL_FACTOR_KEYS)
FUNDAMENTAL_COMPONENT_WEIGHTS = {
    "value": 0.30,
    "quality": 0.35,
    "growth": 0.20,
    "earningsRevision": 0.15,
}
LONG_HALF_LIFE_DAYS = 60.0
SESSION_HALF_LIFE_DAYS = 3.0

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


@dataclass(frozen=True)
class ContinuousRecommendationResult:
    items: list[dict[str, Any]]
    candidate_features: list[dict[str, Any]]
    fundamental_provenance: dict[str, Any]


def resolve_algorithm_version(env_value: str | None, *, enabled: bool, shadow: bool) -> tuple[str, bool]:
    explicit = str(env_value or "").strip().lower()
    if explicit:
        if explicit not in {"legacy", "professional-v1", "continuous-v2"}:
            raise ValueError("RECOMMENDATION_ALGORITHM_VERSION must be legacy, professional-v1, or continuous-v2")
        return explicit, False if explicit == "continuous-v2" else shadow
    return ("professional-v1" if enabled else "legacy"), shadow


def style_prior(style: str) -> dict[str, float]:
    selected = STYLE_WEIGHTS.get(style, STYLE_WEIGHTS["balanced"])
    weights = {key: float(selected[key]) * 0.90 for key in MARKET_FACTOR_KEYS}
    weights.update({key: 2.5 for key in FUNDAMENTAL_FACTOR_KEYS})
    weights = {key: max(0.1, value) for key, value in weights.items()}
    total = sum(weights.values())
    return {key: value / total * 100.0 for key, value in weights.items()}


def initial_preference_state(style: str, cutoff: datetime) -> dict[str, Any]:
    prior = style_prior(style)
    zeros = {key: 0.0 for key in FACTOR_KEYS}
    state = {
        "priorStyle": style if style in STYLE_WEIGHTS else "balanced",
        "priorWeights": prior,
        "longTermLogits": dict(zeros),
        "sessionLogits": dict(zeros),
        "longSampleCount": 0.0,
        "sessionSampleCount": 0.0,
        "asOf": cutoff,
    }
    return finalize_preference_state(state, cutoff)


def process_preference_events(
    previous: dict[str, Any] | None,
    fills: list[dict[str, Any]],
    *,
    style: str,
    cutoff: datetime,
    existing_order_strengths: dict[str, float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = _mutable_preference_state(previous or initial_preference_state(style, cutoff))
    events: list[dict[str, Any]] = []
    order_strengths = dict(existing_order_strengths or {})
    for fill in sorted(fills, key=lambda row: (_aware_datetime(row.get("decision_at"), cutoff), int(row.get("id") or 0))):
        event_time = min(cutoff, _aware_datetime(fill.get("decision_at"), cutoff))
        _decay_state_to(state, event_time)
        event = _preference_event_base(fill)
        if str(fill.get("side") or "").lower() != "buy":
            event.update(event_status="skipped", skip_reason="sell_excluded")
            events.append(event)
            continue
        scores = _number_map(fill.get("feature_scores"))
        means = _number_map(fill.get("candidate_mean_scores"))
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
        remaining = max(0.0, 1.0 - order_strengths.get(order_id, 0.0))
        strength = min(raw_strength, remaining)
        if strength <= 0:
            event.update(event_status="skipped", skip_reason="order_strength_cap_reached")
            events.append(event)
            continue
        relative: dict[str, float] = {}
        for key in FACTOR_KEYS:
            if key not in scores or key not in means:
                continue
            selected = (scores[key] - 50.0) / 50.0
            population = (means[key] - 50.0) / 50.0
            relative[key] = max(-1.0, min(1.0, selected - population))
        if not relative:
            event.update(event_status="skipped", skip_reason="missing_point_in_time_feature")
            events.append(event)
            continue
        for key, exposure in relative.items():
            delta = 0.20 * strength * exposure
            state["longTermLogits"][key] = _clip_logit(state["longTermLogits"].get(key, 0.0) + delta)
            state["sessionLogits"][key] = _clip_logit(state["sessionLogits"].get(key, 0.0) + delta)
        state["longSampleCount"] += strength
        state["sessionSampleCount"] += strength
        order_strengths[order_id] = order_strengths.get(order_id, 0.0) + strength
        event.update(
            event_status="applied",
            skip_reason=None,
            relative_exposure=relative,
            event_strength=strength,
            incremental_notional=notional,
            portfolio_equity=equity or None,
            order_cumulative_strength=order_strengths[order_id],
        )
        events.append(event)
    _decay_state_to(state, cutoff)
    return finalize_preference_state(state, cutoff), events


def finalize_preference_state(state: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    prior = _number_map(state.get("priorWeights")) or style_prior(str(state.get("priorStyle") or "balanced"))
    long_logits = {key: _clip_logit(_number_map(state.get("longTermLogits")).get(key, 0.0)) for key in FACTOR_KEYS}
    session_logits = {key: _clip_logit(_number_map(state.get("sessionLogits")).get(key, 0.0)) for key in FACTOR_KEYS}
    n_session = max(0.0, float(state.get("sessionSampleCount") or 0.0))
    gamma = min(0.5, n_session / (n_session + 3.0))
    logits = {
        key: math.log(max(prior.get(key, 0.1), 0.1) / 100.0) + long_logits[key] + gamma * session_logits[key]
        for key in FACTOR_KEYS
    }
    effective = softmax_weights(logits)
    n_long = max(0.0, float(state.get("longSampleCount") or 0.0))
    confidence = (5.0 + n_long) / (25.0 + n_long)
    result = {
        **state,
        "priorWeights": prior,
        "longTermLogits": long_logits,
        "sessionLogits": session_logits,
        "longSampleCount": n_long,
        "sessionSampleCount": n_session,
        "gamma": gamma,
        "effectiveWeights": effective,
        "preferenceConfidence": confidence,
        "rho": 0.30 * confidence,
        "asOf": cutoff,
        "preferenceModelVersion": PREFERENCE_MODEL_VERSION,
        "factorSchemaVersion": FACTOR_SCHEMA_VERSION,
    }
    result["inputDigest"] = stable_digest(result, omit={"inputDigest"})
    return result


def softmax_weights(logits: dict[str, float]) -> dict[str, float]:
    largest = max(logits.values(), default=0.0)
    exponentials = {key: math.exp(value - largest) for key, value in logits.items()}
    total = sum(exponentials.values()) or 1.0
    return {key: value / total * 100.0 for key, value in exponentials.items()}


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


def infer_risk_state(
    profile: dict[str, Any],
    snapshots: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    *,
    cutoff: datetime,
) -> dict[str, Any]:
    level = str(profile.get("risk_level") or profile.get("riskLevel") or "balanced")
    preset = dict(RISK_PRESETS.get(level, RISK_PRESETS["balanced"]))
    profile_drawdown = _positive(profile.get("max_drawdown_pct") or profile.get("maxDrawdownPct"))
    if profile_drawdown:
        preset["maxDrawdownPct"] = min(preset["maxDrawdownPct"], profile_drawdown)
    reliable = []
    for row in snapshots:
        observed = parse_datetime(row.get("source_as_of") or row.get("sourceAsOf"))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        basis = str(payload.get("valuationBasis") or payload.get("valuation_basis") or "market_value").lower()
        equity = portfolio_equity(payload)
        if observed and cutoff - timedelta(days=90) <= observed <= cutoff and basis != "cost_basis" and equity > 0:
            reliable.append((observed, equity, payload))
    reliable.sort(key=lambda item: item[0])
    daily: dict[str, tuple[datetime, float, dict[str, Any]]] = {}
    for row in reliable:
        daily[row[0].date().isoformat()] = row
    points = list(daily.values())
    spans_days = (points[-1][0] - points[0][0]).days if len(points) >= 2 else 0
    history_ready = len(points) >= 20 and spans_days >= 30
    observed_risk: dict[str, Any] = {}
    evidence = {
        "portfolioHistoryReady": history_ready,
        "marketValueSnapshotCount": len(points),
        "marketValueSpanDays": spans_days,
        "holdingPeriodReady": False,
    }
    effective = dict(preset)
    if history_ready:
        returns = [math.log(current[1] / previous[1]) for previous, current in zip(points, points[1:], strict=False) if previous[1] > 0]
        annual_vol = statistics.pstdev(returns) * math.sqrt(252.0) * 100.0 if len(returns) >= 2 else 0.0
        peak = points[0][1]
        max_drawdown = 0.0
        for _observed, equity, _payload in points:
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0 if peak else 0.0)
        observed_risk.update(annualVolatilityPct=annual_vol, maxDrawdownPct=max_drawdown)
        effective["targetAnnualVolatilityPct"] = min(preset["targetAnnualVolatilityPct"], 1.25 * annual_vol)
        effective["maxDrawdownPct"] = min(preset["maxDrawdownPct"], 1.25 * max_drawdown)
        window_start = cutoff - timedelta(days=30)
        fill_notional = sum(_incremental_notional(row) for row in fills if window_start <= _aware_datetime(row.get("filled_at"), cutoff) <= cutoff)
        recent_equities = [equity for observed, equity, _payload in points if observed >= window_start]
        if recent_equities and fill_notional > 0:
            turnover = fill_notional / statistics.mean(recent_equities) * 100.0
            observed_risk["turnover30dPct"] = turnover
            effective["maximumTurnoverPct"] = min(preset["maximumTurnoverPct"], 1.25 * turnover)
    latest = reliable[-1] if reliable and cutoff - reliable[-1][0] <= timedelta(hours=24) else None
    if latest:
        current = current_portfolio_risk(latest[2])
        observed_risk.update(current)
        effective["maxSingleStockPct"] = min(preset["maxSingleStockPct"], max(5.0, 1.25 * current["maxSingleStockPct"]))
        effective["maxSectorPct"] = min(preset["maxSectorPct"], max(5.0, 1.25 * current["maxSectorPct"]))
        effective["minimumCashPct"] = max(preset["minimumCashPct"], current["cashPct"] / 1.25)
    holding_days = fifo_holding_days(fills, cutoff=cutoff)
    if len(holding_days) >= 3:
        observed_risk["medianHoldingPeriodDays"] = statistics.median(holding_days)
        evidence["holdingPeriodReady"] = True
        evidence["closedLotCount"] = len(holding_days)
    result = {
        "preset": preset,
        "observedRisk": observed_risk,
        "effectiveBudget": effective,
        "evidenceStatus": evidence,
        "dataRanges": {"portfolioDays": 90, "turnoverDays": 30, "holdingDays": 180},
        "riskPolicyVersion": RISK_POLICY_VERSION,
        "asOf": cutoff,
        "portfolioReliable": latest is not None,
    }
    result["inputDigest"] = stable_digest(result, omit={"inputDigest"})
    return result


def apply_continuous_personalization(
    items: list[dict[str, Any]],
    *,
    context: ProfessionalContext,
    preference_state: dict[str, Any],
    risk_state: dict[str, Any],
    fundamental_payload: Any = None,
) -> ContinuousRecommendationResult:
    raw_by_symbol: dict[str, dict[str, float]] = {}
    item_by_symbol = {str(item.get("symbol") or "").upper(): item for item in items}
    for symbol in item_by_symbol:
        raw = raw_factors(symbol, context)
        if raw is not None:
            raw_by_symbol[symbol] = raw
    normalized = cross_sectional_scores(raw_by_symbol)
    fundamentals, provenance = normalize_fundamental_batch(fundamental_payload, list(raw_by_symbol), context.now)
    available_by_symbol: dict[str, dict[str, float]] = {}
    base_by_symbol: dict[str, float] = {}
    fundamental_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, market_scores in normalized.items():
        base_alpha, _ = weighted_score(market_scores, STYLE_WEIGHTS["balanced"])
        fundamental = fundamentals.get(symbol, {"status": "missing", "weight": 0.0, "scores": {}})
        available = dict(market_scores)
        if fundamental.get("status") == "ready" and float(fundamental.get("weight") or 0.0) > 0:
            available.update(_number_map(fundamental.get("scores")))
        available_by_symbol[symbol] = available
        base_by_symbol[symbol] = base_alpha
        fundamental_by_symbol[symbol] = fundamental
    candidate_means = {
        key: statistics.mean([scores[key] for scores in available_by_symbol.values() if key in scores])
        for key in FACTOR_KEYS
        if any(key in scores for scores in available_by_symbol.values())
    }
    feature_rows: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    effective_weights = _number_map(preference_state.get("effectiveWeights")) or style_prior(context.style)
    confidence = float(preference_state.get("preferenceConfidence") or 0.2)
    rho = float(preference_state.get("rho") or 0.30 * confidence)
    budget = _number_map(risk_state.get("effectiveBudget"))
    observed_risk = _number_map(risk_state.get("observedRisk"))
    portfolio_reliable = bool(risk_state.get("portfolioReliable"))
    for symbol, factors in available_by_symbol.items():
        raw = raw_by_symbol[symbol]
        fundamental = fundamental_by_symbol[symbol]
        base_alpha = base_by_symbol[symbol]
        weight = float(fundamental.get("weight") or 0.0)
        fundamental_score = _finite(fundamental.get("score"))
        extended_alpha = (1.0 - weight) * base_alpha + weight * (fundamental_score or 0.0)
        feature_digest = stable_digest({"symbol": symbol, "factors": factors, "means": candidate_means, "cutoff": context.now})
        feature_rows.append({
            "symbol": symbol,
            "evaluated_at": context.now,
            "market_factor_scores": normalized[symbol],
            "fundamental_factor_scores": _number_map(fundamental.get("scores")),
            "available_factor_scores": factors,
            "candidate_mean_scores": candidate_means,
            "base_alpha_score": base_alpha,
            "fundamental_score": fundamental_score,
            "fundamental_weight": weight,
            "fundamental_status": str(fundamental.get("status") or "missing"),
            "feature_snapshot_id": provenance.get("snapshotId"),
            "feature_schema_version": FACTOR_SCHEMA_VERSION,
            "feature_version": FEATURE_VERSION,
            "input_digest": feature_digest,
        })
        if predicted_excess_return(raw) <= 0:
            continue
        item = item_by_symbol[symbol]
        if budget and median_dollar_volume(context.daily_candles_by_symbol.get(symbol) or []) < budget.get("minimumMedianDollarVolume", 0.0):
            continue
        sector_after = post_add_sector_pct(item, context.portfolio_snapshot, context.now)
        if budget and sector_after is not None and sector_after > budget.get("maxSectorPct", 100.0):
            continue
        active_weights = {key: effective_weights.get(key, 0.0) for key in factors}
        weight_total = sum(active_weights.values()) or 1.0
        preference_fit = sum(factors[key] * active_weights[key] / weight_total for key in factors)
        personal_signal = (1.0 - rho) * extended_alpha + rho * preference_fit
        portfolio_fit, portfolio_components, freshness = portfolio_fit_score(symbol, item, context)
        if portfolio_reliable:
            utilization = risk_utilization(item, risk_state, sector_after=sector_after)
            preset_lambda = RISK_BLEND.get(context.risk_level, RISK_BLEND["balanced"])[1]
            portfolio_lambda = min(0.55, preset_lambda + 0.10 * max(0.0, min(1.0, (utilization["riskUtilization"] - 0.8) / 0.2)))
            penalty = bounded_risk_penalty(utilization)
            alpha_weight = 1.0 - portfolio_lambda
        else:
            alpha_weight, portfolio_lambda = RISK_BLEND.get(context.risk_level, RISK_BLEND["balanced"])
            if freshness in {"missing", "stale"}:
                alpha_weight, portfolio_lambda = 1.0, 0.0
            utilization = {"riskUtilization": 0.0}
            penalty = 0.0
        final_score = max(0.0, min(100.0, alpha_weight * personal_signal + portfolio_lambda * portfolio_fit - penalty))
        metrics = dict(item.get("metricsSnapshot") or {})
        metrics.update({
            "algorithmVersion": ALGORITHM_VERSION,
            "baseAlphaScore": round(base_alpha, 4),
            "extendedBaseAlphaScore": round(extended_alpha, 4),
            "fundamentalScore": round(fundamental_score, 4) if fundamental_score is not None else None,
            "fundamentalWeight": round(weight, 8),
            "fundamentalStatus": fundamental.get("status") or "missing",
            "fundamentalProvenance": provenance,
            "preferenceFitScore": round(preference_fit, 4),
            "preferenceConfidence": round(confidence, 8),
            "effectiveWeights": {key: round(value, 6) for key, value in effective_weights.items()},
            "personalizationDelta": round(personal_signal - extended_alpha, 4),
            "portfolioFitScore": round(portfolio_fit, 4),
            "personalScore": round(final_score, 4),
            "riskBudget": risk_state.get("effectiveBudget") or {},
            "observedRisk": risk_state.get("observedRisk") or {},
            "riskUtilization": utilization,
            "riskPenalty": round(penalty, 4),
            "factorContributions": {"portfolio": portfolio_components},
        })
        warnings = list(item.get("riskWarnings") or [])
        if fundamental.get("status") != "ready":
            warnings.append("기업 펀더멘털 데이터가 없어 시장 9개 팩터로 계산했습니다.")
        if not portfolio_reliable:
            warnings.append("신뢰 가능한 포트폴리오 이력이 부족해 기존 위험 결합을 사용했습니다.")
        output.append({
            **item,
            "score": round(final_score, 2),
            "riskWarnings": warnings,
            "metricsSnapshot": metrics,
        })
    output.sort(key=lambda row: (float(row.get("score") or 0.0), float(row.get("confidence") or 0.0)), reverse=True)
    for rank, item in enumerate(output[:15], start=1):
        item["rank"] = rank
    return ContinuousRecommendationResult(output[:15], feature_rows, provenance)


def bounded_risk_penalty(utilization: dict[str, float]) -> float:
    excess = lambda key: max(0.0, min(1.0, float(utilization.get(key, 0.0)) - 1.0))
    return min(30.0, 10.0 * excess("volatilityUse") + 8.0 * excess("turnoverUse") + 6.0 * excess("singleStockUse") + 6.0 * excess("sectorUse"))


def risk_utilization(item: dict[str, Any], risk_state: dict[str, Any], *, sector_after: float | None) -> dict[str, float]:
    budget = _number_map(risk_state.get("effectiveBudget"))
    observed = _number_map(risk_state.get("observedRisk"))
    uses = {
        "singleStockUse": 5.0 / max(budget.get("maxSingleStockPct", 100.0), 1e-9),
        "sectorUse": (sector_after or observed.get("maxSectorPct", 0.0)) / max(budget.get("maxSectorPct", 100.0), 1e-9),
        "volatilityUse": observed.get("annualVolatilityPct", 0.0) / max(budget.get("targetAnnualVolatilityPct", 100.0), 1e-9),
        "turnoverUse": observed.get("turnover30dPct", 0.0) / max(budget.get("maximumTurnoverPct", 100.0), 1e-9),
    }
    uses["riskUtilization"] = max(uses.values())
    return uses


def post_add_sector_pct(item: dict[str, Any], snapshot: dict[str, Any] | None, now: datetime) -> float | None:
    if not snapshot:
        return None
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    observed = parse_datetime(snapshot.get("source_as_of") or payload.get("sourceAsOf") or payload.get("asOf"))
    if observed is None or observed > now or now - observed > timedelta(hours=24):
        return None
    equity = portfolio_equity(payload)
    if equity <= 0:
        return None
    sector = normalize_sector(item.get("sector"))
    sector_value = sum(position_value(row) for row in payload.get("positions", []) if isinstance(row, dict) and normalize_sector(row.get("sector")) == sector)
    return (sector_value + equity * 0.05) / equity * 100.0


def portfolio_equity(payload: dict[str, Any]) -> float:
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    explicit = _positive(payload.get("totalValue") or payload.get("totalEvaluationAmount") or account.get("totalValueForeign") or account.get("totalValue"))
    if explicit:
        return explicit
    positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    invested = sum(position_value(row) for row in positions if isinstance(row, dict))
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cashForeign") or account.get("cash"))
    return invested + cash


def current_portfolio_risk(payload: dict[str, Any]) -> dict[str, float]:
    equity = portfolio_equity(payload)
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    values = [position_value(row) for row in positions]
    single = max(values, default=0.0) / equity * 100.0 if equity else 0.0
    sectors: dict[str, float] = {}
    for row, value in zip(positions, values, strict=False):
        sector = normalize_sector(row.get("sector")) or "Unknown"
        sectors[sector] = sectors.get(sector, 0.0) + value
    sector = max(sectors.values(), default=0.0) / equity * 100.0 if equity else 0.0
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cashForeign") or account.get("cash"))
    return {"maxSingleStockPct": single, "maxSectorPct": sector, "cashPct": cash / equity * 100.0 if equity else 0.0}


def fifo_holding_days(fills: list[dict[str, Any]], *, cutoff: datetime) -> list[float]:
    lots: dict[str, list[list[Any]]] = {}
    closed: list[float] = []
    for row in sorted(fills, key=lambda item: _aware_datetime(item.get("filled_at"), cutoff)):
        filled_at = _aware_datetime(row.get("filled_at"), cutoff)
        if filled_at < cutoff - timedelta(days=180) or filled_at > cutoff:
            continue
        symbol = str(row.get("symbol") or "").upper()
        quantity = _positive(row.get("incremental_filled_qty") or row.get("cumulative_filled_qty"))
        if not symbol or quantity <= 0:
            continue
        if str(row.get("side") or "").lower() == "buy":
            lots.setdefault(symbol, []).append([quantity, filled_at])
            continue
        remaining = quantity
        for lot in lots.setdefault(symbol, []):
            if remaining <= 0:
                break
            matched = min(remaining, lot[0])
            lot_was_closed = matched > 0 and matched >= lot[0]
            if matched > 0:
                lot[0] -= matched
                remaining -= matched
                if lot_was_closed:
                    closed.append(max(0.0, (filled_at - lot[1]).total_seconds() / 86_400.0))
    return closed


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


def _preference_event_base(fill: dict[str, Any]) -> dict[str, Any]:
    return {
        "fill_history_id": fill.get("id"),
        "user_sub": fill.get("user_sub"),
        "order_id": fill.get("order_id"),
        "symbol": fill.get("symbol"),
        "side": str(fill.get("side") or "").lower(),
        "decision_at": fill.get("decision_at"),
        "candidate_run_id": fill.get("candidate_run_id"),
        "candidate_feature_id": fill.get("candidate_feature_id"),
        "event_status": "skipped",
        "skip_reason": None,
        "relative_exposure": {},
        "event_strength": 0.0,
        "incremental_notional": None,
        "portfolio_equity": None,
        "order_cumulative_strength": 0.0,
        "provenance": {"source": "historical_seed"} if fill.get("historical_seed") else {"source": "candidate_feature"},
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "processing_version": PREFERENCE_MODEL_VERSION,
    }


def _mutable_preference_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "priorWeights": dict(_number_map(state.get("priorWeights"))),
        "longTermLogits": dict(_number_map(state.get("longTermLogits"))),
        "sessionLogits": dict(_number_map(state.get("sessionLogits"))),
        "asOf": _aware_datetime(state.get("asOf"), datetime.now(timezone.utc)),
    }


def _decay_state_to(state: dict[str, Any], target: datetime) -> None:
    previous = _aware_datetime(state.get("asOf"), target)
    if target <= previous:
        return
    days = (target - previous).total_seconds() / 86_400.0
    long_decay = 0.5 ** (days / LONG_HALF_LIFE_DAYS)
    session_decay = 0.5 ** (days / SESSION_HALF_LIFE_DAYS)
    for key in FACTOR_KEYS:
        state["longTermLogits"][key] = _clip_logit(state["longTermLogits"].get(key, 0.0) * long_decay)
        state["sessionLogits"][key] = _clip_logit(state["sessionLogits"].get(key, 0.0) * session_decay)
    state["longSampleCount"] = float(state.get("longSampleCount") or 0.0) * long_decay
    state["sessionSampleCount"] = float(state.get("sessionSampleCount") or 0.0) * session_decay
    state["asOf"] = target


def _incremental_notional(row: dict[str, Any]) -> float:
    return _positive(row.get("incremental_filled_qty") or row.get("cumulative_filled_qty")) * _positive(row.get("average_fill_price"))


def _clip_logit(value: float) -> float:
    return max(-2.0, min(2.0, float(value)))


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive(value: Any) -> float:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed > 0 else 0.0


def _number_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, child in value.items():
        parsed = _finite(child)
        if parsed is not None:
            result[str(key)] = parsed
    return result


def _aware_datetime(value: Any, fallback: datetime) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
