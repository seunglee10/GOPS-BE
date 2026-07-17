from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.sectors import normalize_sector


WEIGHTS_VERSION = "professional-personalization-v1"
FACTOR_KEYS = (
    "oneDayRelativeStrength",
    "previousSessionStrength",
    "abnormalDollarVolume",
    "closingLocationValue",
    "lastHourRelativeStrength",
    "high52WeekProximity",
    "newsImpact",
    "liquidityQuality",
    "lowVolatilityQuality",
)
STYLE_WEIGHTS: dict[str, dict[str, float]] = {
    "momentum": dict(zip(FACTOR_KEYS, (25, 10, 20, 10, 10, 10, 10, 5, 0), strict=True)),
    "balanced": dict(zip(FACTOR_KEYS, (20, 10, 15, 10, 10, 10, 10, 5, 10), strict=True)),
    "stable": dict(zip(FACTOR_KEYS, (10, 5, 5, 10, 5, 10, 5, 20, 30), strict=True)),
}
RISK_BLEND = {
    "conservative": (0.55, 0.45),
    "balanced": (0.70, 0.30),
    "aggressive": (0.82, 0.18),
}
PORTFOLIO_FIT_WEIGHTS = {
    "sectorDiversification": 0.30,
    "correlationBenefit": 0.25,
    "marginalVolatility": 0.20,
    "liquidityCashCompatibility": 0.15,
    "drawdownCashBuffer": 0.10,
}


@dataclass(frozen=True)
class ProfessionalContext:
    style: str
    risk_level: str
    daily_candles_by_symbol: dict[str, list[dict[str, Any]]]
    previous_session_candles_by_symbol: dict[str, list[dict[str, Any]]]
    news_by_symbol: dict[str, list[dict[str, Any]]]
    portfolio_snapshot: dict[str, Any] | None
    now: datetime
    weights_version: str = WEIGHTS_VERSION
    style_weights: dict[str, dict[str, float]] | None = None


@dataclass(frozen=True)
class ProfessionalWeightSet:
    version: str
    styles: dict[str, dict[str, float]]


def apply_professional_personalization(
    items: list[dict[str, Any]],
    *,
    context: ProfessionalContext,
    shadow: bool,
) -> list[dict[str, Any]]:
    style = context.style if context.style in STYLE_WEIGHTS else "balanced"
    style_weights = context.style_weights or STYLE_WEIGHTS
    raw_by_symbol: dict[str, dict[str, float]] = {}
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        raw = raw_factors(symbol, context)
        if raw is not None:
            raw_by_symbol[symbol] = raw
    normalized = cross_sectional_scores(raw_by_symbol)
    output: list[dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        raw = raw_by_symbol.get(symbol)
        factors = normalized.get(symbol)
        if raw is None or factors is None:
            continue
        predicted_excess = predicted_excess_return(raw)
        if predicted_excess <= 0:
            continue
        balanced_score, balanced_contributions = weighted_score(factors, style_weights["balanced"])
        style_score, style_contributions = weighted_score(factors, style_weights[style])
        portfolio_fit, portfolio_components, freshness = portfolio_fit_score(symbol, item, context)
        alpha_weight, portfolio_weight = RISK_BLEND.get(context.risk_level, RISK_BLEND["balanced"])
        if freshness in {"missing", "stale"}:
            alpha_weight, portfolio_weight = 1.0, 0.0
        hard_penalty, hard_warnings = hard_penalty_for(item, raw, context)
        personal_score = max(0.0, min(100.0, style_score * alpha_weight + portfolio_fit * portfolio_weight - hard_penalty))
        metrics = dict(item.get("metricsSnapshot") or {})
        metrics.update({
            "baseAlphaScore": round(balanced_score, 4),
            "styleSignalScore": round(style_score, 4),
            "portfolioFitScore": round(portfolio_fit, 4),
            "personalScore": round(personal_score, 4),
            "predictedExcessReturnPct": round(predicted_excess, 6),
            "professionalFactorRaw": {key: round(value, 8) for key, value in raw.items()},
            "professionalFactorScores": {key: round(value, 4) for key, value in factors.items()},
            "factorContributions": {
                "base": balanced_contributions,
                "style": style_contributions,
                "portfolio": portfolio_components,
            },
            "personalization": {
                "enabled": True,
                "shadow": shadow,
                "recommendationStyle": style,
                "riskLevel": context.risk_level,
                "weightsVersion": context.weights_version,
                "alphaWeight": alpha_weight,
                "portfolioWeight": portfolio_weight,
                "portfolioFreshness": freshness,
            },
        })
        reasons = list(item.get("reasons") or [])
        top_factors = sorted(style_contributions.items(), key=lambda entry: entry[1], reverse=True)[:3]
        for factor, contribution in top_factors:
            reasons.append({
                "type": "professional_factor",
                "text": factor_reason(factor, factors[factor]),
                "weight": contribution,
            })
        output.append({
            **item,
            "score": item.get("score") if shadow else round(personal_score, 2),
            "reasons": reasons[:5],
            "riskWarnings": [*(item.get("riskWarnings") or []), *hard_warnings],
            "metricsSnapshot": metrics,
        })
    output.sort(
        key=lambda row: (
            float(row.get("score") or 0) if shadow else float((row.get("metricsSnapshot") or {}).get("personalScore") or 0),
            float(row.get("confidence") or 0),
        ),
        reverse=True,
    )
    for rank, item in enumerate(output[:15], start=1):
        item["rank"] = rank
    return output[:15]


def personalization_digest(
    *, profile: dict[str, Any], portfolio_snapshot: dict[str, Any] | None, shadow: bool,
    weights_version: str = WEIGHTS_VERSION,
    style_weights: dict[str, dict[str, float]] | None = None,
) -> str:
    portfolio = portfolio_snapshot or {}
    material = {
        "style": profile.get("recommendation_style") or profile.get("recommendationStyle") or "balanced",
        "risk": profile.get("risk_level") or profile.get("riskLevel") or "balanced",
        "maxDrawdownPct": profile.get("max_drawdown_pct") or profile.get("maxDrawdownPct"),
        "preferredSectors": sorted(profile.get("preferred_sectors") or profile.get("preferredSectors") or []),
        "excludedSectors": sorted(profile.get("excluded_sectors") or profile.get("excludedSectors") or []),
        "excludedSymbols": sorted(profile.get("excluded_symbols") or profile.get("excludedSymbols") or []),
        "portfolioHistoryId": portfolio.get("id"),
        "portfolioSourceAsOf": portfolio.get("source_as_of") or portfolio.get("sourceAsOf"),
        "weightsVersion": weights_version,
        "weights": style_weights or STYLE_WEIGHTS,
        "shadow": shadow,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve_weight_set(payload: dict[str, Any] | None = None) -> ProfessionalWeightSet:
    if payload is None:
        return ProfessionalWeightSet(version=WEIGHTS_VERSION, styles=STYLE_WEIGHTS)
    version = str(payload.get("version") or "").strip()
    training_cutoff = str(payload.get("trainingCutoff") or "").strip()
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    if not version or not training_cutoff or validation.get("approved") is not True or validation.get("outOfSampleImprovement") is not True:
        raise ValueError("professional weights require version, training cutoff, approval, and out-of-sample improvement")
    supplied = payload.get("styles") if isinstance(payload.get("styles"), dict) else {}
    normalized: dict[str, dict[str, float]] = {}
    for style, priors in STYLE_WEIGHTS.items():
        candidate = supplied.get(style) if isinstance(supplied.get(style), dict) else {}
        if set(candidate) != set(FACTOR_KEYS):
            raise ValueError(f"{style} weights must contain the complete professional factor set")
        weights = {key: float(candidate[key]) for key in FACTOR_KEYS}
        if any(value < 0 or abs(value - priors[key]) > 10 for key, value in weights.items()):
            raise ValueError(f"{style} weights violate nonnegative or ±10 percentage-point constraints")
        if abs(sum(weights.values()) - 100.0) > 1e-6:
            raise ValueError(f"{style} weights must sum to 100")
        normalized[style] = weights
    return ProfessionalWeightSet(version=version, styles=normalized)


def raw_factors(symbol: str, context: ProfessionalContext) -> dict[str, float] | None:
    daily = completed_daily(context.daily_candles_by_symbol.get(symbol) or [], context.now)
    spy_daily = completed_daily(context.daily_candles_by_symbol.get("SPY") or [], context.now)
    if len(daily) < 252 or len(spy_daily) < 252:
        return None
    if not canonical_rows_are_eligible([*daily[-252:], *spy_daily[-252:]]):
        return None
    latest, previous = daily[-1], daily[-2]
    spy_latest, spy_previous = spy_daily[-1], spy_daily[-2]
    latest_return = bar_return(latest)
    spy_latest_return = bar_return(spy_latest)
    two_session_return = open_to_close_return(previous, latest)
    spy_two_session_return = open_to_close_return(spy_previous, spy_latest)
    dollar_volumes = [positive(row.get("close")) * positive(row.get("volume")) for row in daily[-21:-1]]
    median_dollar_volume = statistics.median([value for value in dollar_volumes if value > 0] or [0.0])
    latest_dollar_volume = positive(latest.get("close")) * positive(latest.get("volume"))
    close = positive(latest.get("close"))
    high = positive(latest.get("high"))
    low = positive(latest.get("low"))
    high52 = max((positive(row.get("high")) for row in daily[-252:]), default=0.0)
    returns = close_returns(daily[-21:])
    previous_minutes = context.previous_session_candles_by_symbol.get(symbol) or []
    spy_minutes = context.previous_session_candles_by_symbol.get("SPY") or []
    if len(previous_minutes) < 30 or len(spy_minutes) < 30:
        return None
    news = context.news_by_symbol.get(symbol) or []
    return {
        "oneDayRelativeStrength": latest_return - spy_latest_return,
        "previousSessionStrength": two_session_return - spy_two_session_return,
        "abnormalDollarVolume": math.log(max(latest_dollar_volume, 1.0) / max(median_dollar_volume, 1.0)),
        "closingLocationValue": (2 * close - high - low) / (high - low) if high > low else 0.0,
        "lastHourRelativeStrength": last_hour_return(previous_minutes) - last_hour_return(spy_minutes),
        "high52WeekProximity": close / high52 if high52 else 0.0,
        "newsImpact": news_impact(news, context.now),
        "liquidityQuality": math.log10(max(median_dollar_volume, 1.0)),
        "lowVolatilityQuality": -statistics.pstdev(returns) if len(returns) >= 10 else -99.0,
    }


def cross_sectional_scores(raw_by_symbol: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    result = {symbol: {} for symbol in raw_by_symbol}
    for key in FACTOR_KEYS:
        values = sorted((factors[key], symbol) for symbol, factors in raw_by_symbol.items())
        denominator = max(1, len(values) - 1)
        for index, (_value, symbol) in enumerate(values):
            result[symbol][key] = index / denominator * 100.0 if len(values) > 1 else 50.0
    return result


def weighted_score(scores: dict[str, float], weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    contributions = {key: round(scores[key] * weights[key] / 100.0, 4) for key in FACTOR_KEYS}
    return sum(contributions.values()), contributions


def predicted_excess_return(raw: dict[str, float]) -> float:
    return (
        0.35 * raw["oneDayRelativeStrength"]
        + 0.20 * raw["previousSessionStrength"]
        + 0.15 * raw["lastHourRelativeStrength"]
        + 0.10 * raw["closingLocationValue"]
        + 0.08 * raw["abnormalDollarVolume"]
        + 0.07 * raw["newsImpact"]
        + 0.05 * (raw["high52WeekProximity"] - 0.8)
    )


def portfolio_fit_score(
    symbol: str, item: dict[str, Any], context: ProfessionalContext
) -> tuple[float, dict[str, float], str]:
    snapshot = context.portfolio_snapshot
    if not snapshot:
        neutral = {key: 50.0 for key in PORTFOLIO_FIT_WEIGHTS}
        return 50.0, neutral, "missing"
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    observed = parse_datetime(snapshot.get("source_as_of") or payload.get("sourceAsOf") or payload.get("asOf"))
    age_seconds = (context.now - observed).total_seconds() if observed else float("inf")
    freshness = "fresh" if age_seconds <= 900 else "limited" if age_seconds <= 86_400 else "stale"
    if freshness == "stale":
        neutral = {key: 50.0 for key in PORTFOLIO_FIT_WEIGHTS}
        return 50.0, neutral, freshness
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    values = [position_value(row) for row in positions]
    invested = sum(values)
    candidate_sector = normalize_sector(item.get("sector"))
    sector_value = sum(value for row, value in zip(positions, values, strict=False) if normalize_sector(row.get("sector")) == candidate_sector)
    sector_weight = sector_value / invested if invested else 0.0
    sector_score = max(0.0, min(100.0, 100.0 * (1.0 - sector_weight / 0.5)))
    candidate_return_map = close_return_map(completed_daily(context.daily_candles_by_symbol.get(symbol) or [], context.now)[-61:])
    portfolio_return_map = weighted_portfolio_return_map(positions, context)
    common_dates = sorted(set(candidate_return_map).intersection(portfolio_return_map))
    candidate_returns = [candidate_return_map[day] for day in common_dates]
    portfolio_returns = [portfolio_return_map[day] for day in common_dates]
    correlation = correlation_of(candidate_returns, portfolio_returns)
    correlation_score = max(0.0, min(100.0, (1.0 - correlation) * 50.0))
    candidate_vol = statistics.pstdev(candidate_returns) if len(candidate_returns) >= 10 else 0.05
    portfolio_vol = statistics.pstdev(portfolio_returns) if len(portfolio_returns) >= 10 else candidate_vol
    marginal_score = max(0.0, min(100.0, 50.0 + (portfolio_vol - candidate_vol) * 2_000.0))
    median_adv = median_dollar_volume(context.daily_candles_by_symbol.get(symbol) or [])
    liquidity_score = max(0.0, min(100.0, (math.log10(max(median_adv, 1.0)) - 5.0) / 4.0 * 100.0))
    cash = positive(payload.get("cash") or payload.get("cashForeign") or (payload.get("account") or {}).get("cash"))
    total = positive(payload.get("totalValue") or payload.get("totalEvaluationAmount") or (payload.get("account") or {}).get("totalValue")) or invested + cash
    cash_ratio = cash / total if total else 0.0
    cash_component = 50.0 if freshness == "limited" else max(0.0, min(100.0, cash_ratio / 0.15 * 100.0))
    liquidity_cash = (liquidity_score + cash_component) / 2.0
    drawdown_buffer = 50.0 if freshness == "limited" else max(0.0, min(100.0, 35.0 + cash_ratio * 300.0))
    components = {
        "sectorDiversification": sector_score,
        "correlationBenefit": correlation_score,
        "marginalVolatility": marginal_score,
        "liquidityCashCompatibility": liquidity_cash,
        "drawdownCashBuffer": drawdown_buffer,
    }
    score = sum(components[key] * weight for key, weight in PORTFOLIO_FIT_WEIGHTS.items())
    return score, {key: round(value, 4) for key, value in components.items()}, freshness


def hard_penalty_for(item: dict[str, Any], raw: dict[str, float], context: ProfessionalContext) -> tuple[float, list[str]]:
    warnings: list[str] = []
    penalty = 0.0
    if raw["liquidityQuality"] < 6.0:
        penalty += 20.0
        warnings.append("20일 중앙 거래대금이 전문 추천 유동성 기준보다 낮습니다.")
    if raw["lowVolatilityQuality"] < -0.08 and context.risk_level == "conservative":
        penalty += 15.0
        warnings.append("보수형 위험성향에 비해 최근 변동성이 높습니다.")
    sector_weight = portfolio_sector_weight(item, context.portfolio_snapshot, context.now)
    hard_cap = {"conservative": 0.45, "balanced": 0.55, "aggressive": 0.65}.get(context.risk_level, 0.55)
    if sector_weight >= hard_cap:
        penalty += 35.0
        warnings.append(f"현재 포트폴리오의 동일 섹터 비중이 {sector_weight * 100:.1f}%로 hard cap을 넘습니다.")
    return penalty, warnings


def factor_reason(factor: str, score: float) -> str:
    labels = {
        "oneDayRelativeStrength": "최근 거래일 SPY 대비 상대수익률",
        "previousSessionStrength": "직전 거래일 시가부터 최근 종가까지의 SPY 대비 강도",
        "abnormalDollarVolume": "20일 기준 비정상 거래대금",
        "closingLocationValue": "종가의 일중 범위 내 위치",
        "lastHourRelativeStrength": "마지막 1시간 SPY 대비 강도",
        "high52WeekProximity": "52주 고가 근접도",
        "newsImpact": "cutoff-safe 뉴스 영향",
        "liquidityQuality": "20일 중앙 거래대금 유동성",
        "lowVolatilityQuality": "20일 저변동성 품질",
    }
    return f"{labels.get(factor, factor)}의 횡단면 점수가 {score:.1f}/100입니다."


def completed_daily(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    market_now = now.astimezone(ZoneInfo("America/New_York"))
    current_market_date = market_now.date()
    regular_session_closed = (market_now.hour, market_now.minute) >= (16, 0)
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or row.get("eventTime") or ""))
    result = []
    for row in ordered:
        observed = parse_datetime(row.get("timestamp") or row.get("eventTime"))
        observed_date = observed.date() if observed else None
        explicitly_closed = row.get("isClosed") is True or row.get("is_closed") is True
        if observed_date and observed_date > current_market_date:
            continue
        if observed_date == current_market_date and not (regular_session_closed and explicitly_closed):
            continue
        if row.get("isClosed") is False or row.get("is_closed") is False:
            continue
        result.append(row)
    return result


def bar_return(row: dict[str, Any]) -> float:
    opened, closed = positive(row.get("open")), positive(row.get("close"))
    return (closed / opened - 1.0) * 100.0 if opened else 0.0


def open_to_close_return(opening_bar: dict[str, Any], closing_bar: dict[str, Any]) -> float:
    opened, closed = positive(opening_bar.get("open")), positive(closing_bar.get("close"))
    return (closed / opened - 1.0) * 100.0 if opened else 0.0


def close_returns(rows: list[dict[str, Any]]) -> list[float]:
    closes = [positive(row.get("close")) for row in rows]
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes)) if closes[index - 1] > 0]


def last_hour_return(rows: list[dict[str, Any]]) -> float:
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or row.get("eventTime") or ""))[-60:]
    if len(ordered) < 30:
        return 0.0
    opened = positive(ordered[0].get("open") or ordered[0].get("close"))
    closed = positive(ordered[-1].get("close"))
    return (closed / opened - 1.0) * 100.0 if opened else 0.0


def news_impact(rows: list[dict[str, Any]], now: datetime) -> float:
    score = 0.0
    seen: set[str] = set()
    for row in rows:
        identity = str(row.get("articleId") or row.get("id") or row.get("url") or row.get("headline") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        observed = parse_datetime(
            row.get("availableAt")
            or row.get("receivedAt")
            or row.get("received_at")
            or row.get("localizedAt")
            or row.get("publishedAt")
            or row.get("published_at")
        )
        if observed is None or observed > now:
            continue
        age_hours = max(0.0, (now - observed).total_seconds() / 3600.0)
        decay = math.exp(-age_hours / 48.0)
        sentiment = str(row.get("impactDirection") or row.get("sentiment") or "").lower()
        direction = 1.0 if sentiment in {"positive", "bullish", "up"} else -1.0 if sentiment in {"negative", "bearish", "down"} else 0.0
        score += direction * decay
    return max(-1.0, min(1.0, score))


def weighted_portfolio_return_map(positions: list[dict[str, Any]], context: ProfessionalContext) -> dict[str, float]:
    series: list[dict[str, float]] = []
    weights = []
    for position in positions:
        symbol = str(position.get("symbol") or "").upper()
        returns = close_return_map(completed_daily(context.daily_candles_by_symbol.get(symbol) or [], context.now)[-61:])
        value = position_value(position)
        if len(returns) >= 10 and value > 0:
            series.append(returns)
            weights.append(value)
    if not series:
        return {}
    common_dates = set(series[0])
    for row in series[1:]:
        common_dates.intersection_update(row)
    total = sum(weights)
    return {
        day: sum(row[day] * weight / total for row, weight in zip(series, weights, strict=True))
        for day in sorted(common_dates)
    }


def close_return_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or row.get("eventTime") or ""))
    result: dict[str, float] = {}
    for previous, current in zip(ordered, ordered[1:], strict=False):
        previous_close = positive(previous.get("close"))
        current_close = positive(current.get("close"))
        observed = parse_datetime(current.get("timestamp") or current.get("eventTime"))
        if previous_close and current_close and observed:
            result[observed.date().isoformat()] = current_close / previous_close - 1.0
    return result


def correlation_of(left: list[float], right: list[float]) -> float:
    length = min(len(left), len(right))
    if length < 10:
        return 0.0
    left, right = left[-length:], right[-length:]
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    denominator = math.sqrt(sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right))
    return numerator / denominator if denominator else 0.0


def median_dollar_volume(rows: list[dict[str, Any]]) -> float:
    values = [positive(row.get("close")) * positive(row.get("volume")) for row in rows[-20:]]
    return statistics.median(values) if values else 0.0


def position_value(row: dict[str, Any]) -> float:
    return positive(row.get("marketValueKrw") or row.get("marketValueForeign") or row.get("marketValue"))


def portfolio_sector_weight(item: dict[str, Any], snapshot: dict[str, Any] | None, now: datetime) -> float:
    if not snapshot:
        return 0.0
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else snapshot
    observed = parse_datetime(snapshot.get("source_as_of") or payload.get("sourceAsOf") or payload.get("asOf"))
    if observed is None or observed > now or now - observed > timedelta(hours=24):
        return 0.0
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    values = [position_value(row) for row in positions]
    total = sum(values)
    sector = normalize_sector(item.get("sector"))
    matching = sum(value for row, value in zip(positions, values, strict=False) if normalize_sector(row.get("sector")) == sector)
    return matching / total if total else 0.0


def canonical_rows_are_eligible(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        adjustment = str(row.get("price_adjustment") or row.get("priceAdjustment") or "").strip().lower()
        if adjustment and adjustment != "split":
            return False
        session = str(row.get("market_session") or row.get("marketSession") or "").strip().lower()
        if session and session != "regular":
            return False
    return True


def positive(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
