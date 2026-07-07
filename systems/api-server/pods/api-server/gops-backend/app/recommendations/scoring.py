from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")
MIN_CANDLE_COUNT = 60
MIN_SESSION_DOLLAR_VOLUME = 10_000_000


@dataclass(frozen=True)
class RecommendationProfile:
    risk_level: str
    horizon: str
    max_drawdown_pct: float
    preferred_sectors: tuple[str, ...] = ()
    excluded_sectors: tuple[str, ...] = ()
    excluded_symbols: tuple[str, ...] = ()


@dataclass
class Candidate:
    symbol: str
    sector: str = "Unclassified"
    industry: str = "Unclassified"
    source: str = "market_rank"
    session_dollar_volume: float = 0.0
    change_percent: float | None = None
    last_price: float | None = None


@dataclass
class RecommendationInput:
    profile: RecommendationProfile
    watchlist_symbols: list[str]
    portfolio_positions: list[dict[str, Any]]
    market_items: list[dict[str, Any]]
    candles_by_symbol: dict[str, list[dict[str, Any]]]
    spy_candles: list[dict[str, Any]] = field(default_factory=list)
    active_symbol: str | None = None
    news_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def is_regular_market_open(now: datetime) -> bool:
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return False
    current = local.time()
    return time(9, 30) <= current < time(16, 0)


def recommendation_slot(now: datetime) -> dict[str, str]:
    local = now.astimezone(MARKET_TZ)
    market_date = local.date().isoformat()
    anchors = [time(9, 45), time(12, 45), time(15, 45)]
    selected = anchors[0]
    for anchor in anchors:
        if local.time() >= anchor:
            selected = anchor
    slot = local.replace(hour=selected.hour, minute=selected.minute, second=0, microsecond=0)
    return {
        "marketDate": market_date,
        "slotStart": slot.isoformat(),
    }


def score_recommendations(payload: RecommendationInput) -> list[dict[str, Any]]:
    candidates = build_candidates(
        watchlist_symbols=payload.watchlist_symbols,
        portfolio_positions=payload.portfolio_positions,
        preferred_sectors=list(payload.profile.preferred_sectors),
        market_items=payload.market_items,
    )
    spy_return = return_pct(payload.spy_candles)
    portfolio = portfolio_summary(payload.portfolio_positions)
    excluded_symbols = candidate_exclusion_symbols(payload)
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        item = score_candidate(candidate, payload, spy_return, portfolio, excluded_symbols)
        if item is not None:
            scored.append(item)
    scored.sort(key=lambda item: (item["score"], item["confidence"]), reverse=True)
    diversified = enforce_sector_diversity(scored, max_items=5, max_per_sector=2)
    for index, item in enumerate(diversified, start=1):
        item["rank"] = index
    return diversified


def build_candidates(
    *,
    watchlist_symbols: list[str],
    portfolio_positions: list[dict[str, Any]],
    preferred_sectors: list[str],
    market_items: list[dict[str, Any]],
    max_candidates: int = 50,
) -> list[Candidate]:
    by_symbol: dict[str, Candidate] = {}
    priority = {"related_sector": 2, "market_rank": 1}
    market_by_symbol = {normalize_symbol(item.get("symbol")): item for item in market_items if normalize_symbol(item.get("symbol"))}
    context_symbols = {
        normalize_symbol(value)
        for value in [*watchlist_symbols, *[str(item.get("symbol") or "") for item in portfolio_positions]]
        if normalize_symbol(value)
    }

    def add(symbol: str, source: str, item: dict[str, Any] | None = None) -> None:
        normalized = normalize_symbol(symbol)
        if not normalized or normalized in context_symbols or normalized == "SPY":
            return
        source_item = item or market_by_symbol.get(normalized) or {}
        candidate = Candidate(
            symbol=normalized,
            sector=str(source_item.get("sector") or "Unclassified"),
            industry=str(source_item.get("industry") or "Unclassified"),
            source=source,
            session_dollar_volume=as_float(source_item.get("sessionDollarVolume")) or 0.0,
            change_percent=as_float(source_item.get("changePercent")),
            last_price=as_float(source_item.get("lastPrice")),
        )
        existing = by_symbol.get(normalized)
        if existing is None or priority.get(source, 0) > priority.get(existing.source, 0):
            by_symbol[normalized] = candidate

    ranked = sorted(
        market_items,
        key=lambda item: as_float(item.get("sessionDollarVolume")) or 0.0,
        reverse=True,
    )
    for item in ranked[:30]:
        add(str(item.get("symbol") or ""), "market_rank", item)
    movers = sorted(market_items, key=lambda item: abs(as_float(item.get("changePercent")) or 0.0), reverse=True)
    for item in movers[:20]:
        add(str(item.get("symbol") or ""), "market_rank", item)

    anchor_sectors = {
        (market_by_symbol.get(normalize_symbol(symbol)) or {}).get("sector")
        for symbol in [*watchlist_symbols, *[str(item.get("symbol") or "") for item in portfolio_positions]]
    }
    anchor_sectors = {str(sector) for sector in anchor_sectors if sector}
    anchor_sectors.update(str(sector) for sector in preferred_sectors if str(sector).strip())
    per_sector: dict[str, int] = {}
    for item in ranked:
        sector = str(item.get("sector") or "")
        if sector not in anchor_sectors:
            continue
        if per_sector.get(sector, 0) >= 3:
            continue
        add(str(item.get("symbol") or ""), "related_sector", item)
        per_sector[sector] = per_sector.get(sector, 0) + 1

    return list(by_symbol.values())[:max_candidates]


def score_candidate(
    candidate: Candidate,
    payload: RecommendationInput,
    spy_return: float,
    portfolio: dict[str, Any],
    excluded_symbols: set[str],
) -> dict[str, Any] | None:
    profile = payload.profile
    excluded_sectors = {normalize_text(item) for item in profile.excluded_sectors}
    if candidate.symbol in excluded_symbols or normalize_text(candidate.sector) in excluded_sectors:
        return None

    candles = payload.candles_by_symbol.get(candidate.symbol) or []
    if len(candles) < MIN_CANDLE_COUNT:
        return None
    metrics = calculate_metrics(candles, spy_return, candidate)
    if metrics["latestClose"] is None or metrics["sessionDollarVolume"] < MIN_SESSION_DOLLAR_VOLUME:
        return None
    if metrics["intradayRangePct"] > profile.max_drawdown_pct * 1.5:
        return None
    max_weight = {"conservative": 15.0, "balanced": 25.0, "aggressive": 35.0}.get(profile.risk_level, 25.0)
    position_weight = portfolio["weights"].get(candidate.symbol, 0.0)
    if position_weight > max_weight:
        return None
    sector_weight = portfolio["sector_weights"].get(candidate.sector, 0.0)
    sector_caps = sector_risk_caps(profile.risk_level)
    if sector_weight >= sector_caps["hard"]:
        return None
    if profile.risk_level == "conservative" and metrics["return3hPct"] > 5:
        return None

    reasons: list[dict[str, Any]] = []
    risks: list[str] = []
    alpha_score = alpha_points(metrics, reasons)
    catalyst_score = catalyst_points(candidate, payload.news_by_symbol.get(candidate.symbol) or [], metrics, reasons)
    execution_score = execution_points(metrics, reasons, risks)
    risk_penalty = portfolio_risk_penalty(candidate, portfolio, sector_caps, risks)

    if profile.risk_level == "conservative":
        alpha_score = max(0.0, alpha_score - 10.0)
        execution_score = min(25.0, execution_score + 5.0)
    elif profile.risk_level == "aggressive":
        alpha_score = min(60.0, alpha_score + 5.0)
    elif profile.risk_level == "balanced" and metrics["return3hPct"] > 8:
        risk_penalty += 5.0
        risks.append("최근 3시간 급등 폭이 커 추격 매수 리스크가 있습니다.")

    if profile.risk_level == "aggressive" and metrics["return3hPct"] > 10:
        risk_penalty += 4.0
        risks.append("공격형 프로필 기준으로 급등 종목을 제외하지 않았지만 변동성 관리가 필요합니다.")

    score_breakdown = {
        "alpha": round(alpha_score, 4),
        "catalyst": round(catalyst_score, 4),
        "execution": round(execution_score, 4),
        "riskPenalty": round(risk_penalty, 4),
    }
    score = round(max(0.0, min(100.0, alpha_score + catalyst_score + execution_score - risk_penalty)), 2)
    confidence = confidence_for(metrics, reasons)
    if score < 75 or confidence < 0.7 or len(reasons) < 2:
        return None
    metrics_snapshot = {
        **metrics,
        "source": candidate.source,
        "sectorWeight": round(sector_weight, 6),
        "excludedReason": None,
        "scoreBreakdown": score_breakdown,
    }
    return {
        "symbol": candidate.symbol,
        "action": "buy",
        "rank": 0,
        "score": score,
        "confidence": confidence,
        "sector": candidate.sector,
        "reasons": reasons[:5],
        "riskWarnings": risks,
        "metricsSnapshot": metrics_snapshot,
    }


def calculate_metrics(candles: list[dict[str, Any]], spy_return: float, candidate: Candidate) -> dict[str, Any]:
    ordered = sorted(candles, key=lambda item: str(item.get("timestamp") or ""))
    latest = ordered[-1]
    first = ordered[0]
    close_now = as_float(latest.get("close"))
    close_start = as_float(first.get("close")) or as_float(first.get("open"))
    session_open = as_float(ordered[0].get("open")) or close_start
    highs = [as_float(item.get("high")) for item in ordered if as_float(item.get("high")) is not None]
    lows = [as_float(item.get("low")) for item in ordered if as_float(item.get("low")) is not None]
    last60 = ordered[-60:]
    prev60 = ordered[-120:-60] if len(ordered) >= 120 else ordered[: max(1, len(ordered) - 60)]
    last60_volume = sum(as_float(item.get("volume")) or 0.0 for item in last60)
    prev60_volume = sum(as_float(item.get("volume")) or 0.0 for item in prev60)
    previous_highs = [as_float(item.get("high")) for item in ordered[:-15] if as_float(item.get("high")) is not None]
    return3h = ((close_now - close_start) / close_start * 100.0) if close_now is not None and close_start else 0.0
    session_high = max(highs) if highs else close_now
    session_low = min(lows) if lows else close_now
    intraday_range = ((session_high - session_low) / session_open * 100.0) if session_high and session_low and session_open else 0.0
    volume_ratio = (last60_volume / prev60_volume) if prev60_volume > 0 else (2.0 if last60_volume > 0 else 0.0)
    session_dollar_volume = candidate.session_dollar_volume or (close_now or 0.0) * sum(as_float(item.get("volume")) or 0.0 for item in ordered)
    return {
        "latestClose": round(close_now, 6) if close_now is not None else None,
        "return3hPct": round(return3h, 6),
        "relativeStrength": round(return3h - spy_return, 6),
        "volumeRatio": round(volume_ratio, 6),
        "breakout": bool(close_now is not None and previous_highs and close_now >= max(previous_highs)),
        "intradayRangePct": round(intraday_range, 6),
        "sessionDollarVolume": round(session_dollar_volume, 2),
        "candleCount": len(ordered),
        "dataFreshness": str(latest.get("timestamp") or ""),
    }


def alpha_points(metrics: dict[str, Any], reasons: list[dict[str, Any]]) -> float:
    score = 0.0
    ret = float(metrics["return3hPct"])
    rel = float(metrics["relativeStrength"])
    vol = float(metrics["volumeRatio"])
    if ret > 0:
        pts = min(18.0, ret / 4.0 * 18.0)
        score += pts
        reasons.append(reason("momentum", f"최근 3시간 수익률이 {ret:.2f}%로 양호합니다.", pts))
    if rel > 0:
        pts = min(18.0, rel / 3.0 * 18.0)
        score += pts
        reasons.append(reason("relative_strength", f"SPY 대비 상대강도가 {rel:.2f}%p 높습니다.", pts))
    if vol >= 1.2:
        pts = min(12.0, (vol - 1.0) / 1.5 * 12.0)
        score += pts
        reasons.append(reason("volume", f"최근 60분 거래량이 직전 구간 대비 {vol:.2f}배입니다.", pts))
    if metrics["breakout"]:
        score += 12.0
        reasons.append(reason("breakout", "최근 3시간 전고점 돌파 신호가 있습니다.", 12.0))
    return score


def catalyst_points(candidate: Candidate, news_items: list[dict[str, Any]], metrics: dict[str, Any], reasons: list[dict[str, Any]]) -> float:
    if not news_items:
        return 0.0
    score = 8.0
    reasons.append(reason("catalyst", "최근 관련 뉴스 또는 이벤트 근거가 있습니다.", 8))
    if metrics["volumeRatio"] >= 1.5:
        score += 5
        reasons.append(reason("catalyst_volume", "뉴스와 거래량 증가가 함께 관측됩니다.", 5))
    negative = any(str(item.get("sentiment") or item.get("impactDirection") or "").lower() in {"negative", "bearish"} for item in news_items)
    if not negative:
        score += 2
    else:
        score -= 5
    return max(0.0, score)


def execution_points(metrics: dict[str, Any], reasons: list[dict[str, Any]], risks: list[str]) -> float:
    score = 0.0
    if metrics["intradayRangePct"] <= 4:
        score += 8
        reasons.append(reason("execution_risk", "장중 변동폭이 과도하지 않아 진입 가격 관리가 가능합니다.", 8))
    else:
        risks.append("장중 변동성이 커 매수 가격 관리가 필요합니다.")
    if metrics["sessionDollarVolume"] >= MIN_SESSION_DOLLAR_VOLUME * 5:
        score += 7
        reasons.append(reason("liquidity", "세션 거래대금이 충분해 장중 체결 부담이 낮습니다.", 7))
    if metrics["dataFreshness"]:
        score += 5
    if metrics["return3hPct"] <= 6:
        score += 5
    else:
        risks.append("단기 상승 폭이 커 단기 되돌림 리스크가 있습니다.")
    return score


def confidence_for(metrics: dict[str, Any], reasons: list[dict[str, Any]]) -> float:
    confidence = 0.45
    confidence += min(0.2, max(0, metrics["candleCount"] - 60) / 180 * 0.2)
    confidence += 0.15 if metrics["sessionDollarVolume"] >= MIN_SESSION_DOLLAR_VOLUME * 5 else 0.08
    confidence += min(0.2, len(reasons) * 0.04)
    return round(min(0.95, confidence), 4)


def portfolio_summary(positions: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, float] = {}
    sector_values: dict[str, float] = {}
    total = 0.0
    cash = 0.0
    for position in positions:
        symbol = normalize_symbol(position.get("symbol"))
        value = as_float(position.get("marketValueKrw")) or as_float(position.get("marketValueForeign")) or 0.0
        sector = str(position.get("sector") or "Unclassified")
        if symbol and value > 0:
            values[symbol] = values.get(symbol, 0.0) + value
            sector_values[sector] = sector_values.get(sector, 0.0) + value
            total += value
    weights = {symbol: (value / total * 100.0) for symbol, value in values.items()} if total else {}
    sector_weights = {sector: (value / total * 100.0) for sector, value in sector_values.items()} if total else {}
    return {"weights": weights, "sector_weights": sector_weights, "cash_available": cash >= 0}


def candidate_exclusion_symbols(payload: RecommendationInput) -> set[str]:
    symbols = {normalize_symbol(item) for item in payload.watchlist_symbols}
    symbols.update(normalize_symbol(item.get("symbol")) for item in payload.portfolio_positions)
    symbols.update(normalize_symbol(item) for item in payload.profile.excluded_symbols)
    if payload.active_symbol:
        symbols.add(normalize_symbol(payload.active_symbol))
    symbols.add("SPY")
    return {symbol for symbol in symbols if symbol}


def sector_risk_caps(risk_level: str) -> dict[str, float]:
    if risk_level == "conservative":
        return {"soft": 30.0, "hard": 45.0}
    if risk_level == "aggressive":
        return {"soft": 50.0, "hard": 65.0}
    return {"soft": 40.0, "hard": 55.0}


def portfolio_risk_penalty(candidate: Candidate, portfolio: dict[str, Any], sector_caps: dict[str, float], risks: list[str]) -> float:
    sector_weight = portfolio["sector_weights"].get(candidate.sector, 0.0)
    if sector_weight < sector_caps["soft"]:
        return 0.0
    excess = sector_weight - sector_caps["soft"]
    penalty = min(15.0, 6.0 + excess / max(1.0, sector_caps["hard"] - sector_caps["soft"]) * 9.0)
    risks.append(f"현재 포트폴리오의 {candidate.sector} 섹터 비중이 {sector_weight:.1f}%로 높아 신규 비중은 제한해야 합니다.")
    return penalty


def enforce_sector_diversity(items: list[dict[str, Any]], *, max_items: int, max_per_sector: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in items:
        sector = str(item.get("sector") or "Unclassified")
        if counts.get(sector, 0) >= max_per_sector:
            continue
        selected.append(item)
        counts[sector] = counts.get(sector, 0) + 1
        if len(selected) >= max_items:
            break
    return selected


def return_pct(candles: list[dict[str, Any]]) -> float:
    if len(candles) < 2:
        return 0.0
    ordered = sorted(candles, key=lambda item: str(item.get("timestamp") or ""))
    start = as_float(ordered[0].get("close")) or as_float(ordered[0].get("open"))
    end = as_float(ordered[-1].get("close"))
    return ((end - start) / start * 100.0) if start and end is not None else 0.0


def reason(reason_type: str, text: str, weight: float) -> dict[str, Any]:
    return {"type": reason_type, "text": text, "weight": round(float(weight), 4)}


def normalize_profile(row: dict[str, Any]) -> RecommendationProfile:
    return RecommendationProfile(
        risk_level=str(row.get("risk_level") or row.get("riskLevel") or "balanced"),
        horizon=str(row.get("horizon") or "intraday"),
        max_drawdown_pct=float(row.get("max_drawdown_pct") or row.get("maxDrawdownPct") or 5),
        preferred_sectors=tuple(str(item) for item in row.get("preferred_sectors") or row.get("preferredSectors") or []),
        excluded_sectors=tuple(str(item) for item in row.get("excluded_sectors") or row.get("excludedSectors") or []),
        excluded_symbols=tuple(normalize_symbol(item) for item in row.get("excluded_symbols") or row.get("excludedSymbols") or [] if normalize_symbol(item)),
    )


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def recent_window_start(now: datetime) -> datetime:
    return now - timedelta(hours=3)
