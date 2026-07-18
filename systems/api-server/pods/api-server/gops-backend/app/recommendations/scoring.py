from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.sectors import sector_label_ko, sector_payload_fields, normalize_sector, normalize_sector_list


MARKET_TZ = ZoneInfo("America/New_York")
MIN_CANDLE_COUNT = 60
REGULAR_OPENING_MIN_CANDLE_COUNT = 30
PRE_MIN_CANDLE_COUNT = 10
MIN_SESSION_DOLLAR_VOLUME = 10_000_000
PRE_MIN_SESSION_DOLLAR_VOLUME = 500_000
NEWS_LOOKBACK_DAYS = 7
SESSION_MODES = {"pre", "regular"}


@dataclass(frozen=True)
class RecommendationProfile:
    risk_level: str
    recommendation_style: str
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
    session_mode: str = "regular"
    news_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def normalize_session_mode(value: Any) -> str:
    normalized = str(value or "regular").strip().lower()
    return normalized if normalized in SESSION_MODES else "regular"


def market_session(now: datetime) -> str:
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return "closed"
    current = local.time()
    if time(4, 0) <= current < time(9, 30):
        return "pre"
    if time(9, 30) <= current < time(16, 0):
        return "regular"
    return "closed"


def is_market_session_open(now: datetime, session_mode: str) -> bool:
    return market_session(now) == normalize_session_mode(session_mode)


def is_regular_market_open(now: datetime) -> bool:
    return is_market_session_open(now, "regular")


def recommendation_slot(now: datetime, session_mode: str = "regular") -> dict[str, str]:
    local = now.astimezone(MARKET_TZ)
    market_date = local.date().isoformat()
    minute = 30 if local.minute >= 30 else 0
    slot = local.replace(minute=minute, second=0, microsecond=0)
    return {
        "marketDate": market_date,
        "slotStart": slot.isoformat(),
        "sessionMode": normalize_session_mode(session_mode),
    }


def score_recommendations(payload: RecommendationInput, *, limit: int | None = None, allow_fallback: bool = True) -> list[dict[str, Any]]:
    session_mode = normalize_session_mode(payload.session_mode)
    candidates = build_candidates(
        watchlist_symbols=payload.watchlist_symbols,
        portfolio_positions=payload.portfolio_positions,
        preferred_sectors=list(payload.profile.preferred_sectors),
        market_items=payload.market_items,
    )
    spy_return = return_pct(filter_candles_for_session(payload.spy_candles, session_mode, payload.now))
    portfolio = portfolio_summary(payload.portfolio_positions)
    excluded_symbols = candidate_exclusion_symbols(payload)
    scored: list[dict[str, Any]] = []
    scored_symbols: set[str] = set()
    for candidate in candidates:
        item = score_candidate(candidate, payload, spy_return, portfolio, excluded_symbols)
        if item is not None:
            scored.append(item)
            scored_symbols.add(candidate.symbol)
    target_count = limit if limit is not None else len(candidates)
    if allow_fallback and len(scored) < target_count:
        for candidate in candidates:
            if candidate.symbol in scored_symbols:
                continue
            item = score_candidate_from_market_snapshot(candidate, payload, portfolio, excluded_symbols)
            if item is not None:
                scored.append(item)
                scored_symbols.add(candidate.symbol)
    scored.sort(key=lambda item: (item["score"], item["confidence"]), reverse=True)
    selected = scored if limit is None else scored[:limit]
    for index, item in enumerate(selected, start=1):
        item["rank"] = index
    return selected


def build_candidates(
    *,
    watchlist_symbols: list[str],
    portfolio_positions: list[dict[str, Any]],
    preferred_sectors: list[str],
    market_items: list[dict[str, Any]],
    max_candidates: int = 500,
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
            sector=normalize_sector(source_item.get("sector")),
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
        normalize_sector((market_by_symbol.get(normalize_symbol(symbol)) or {}).get("sector"))
        for symbol in [*watchlist_symbols, *[str(item.get("symbol") or "") for item in portfolio_positions]]
    }
    anchor_sectors = {sector for sector in anchor_sectors if sector != "Unclassified"}
    anchor_sectors.update(normalize_sector_list(preferred_sectors))
    per_sector: dict[str, int] = {}
    for item in ranked:
        sector = normalize_sector(item.get("sector"))
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
    session_mode = normalize_session_mode(payload.session_mode)
    excluded_sectors = set(normalize_sector_list(list(profile.excluded_sectors)))
    if candidate.symbol in excluded_symbols or normalize_sector(candidate.sector) in excluded_sectors:
        return None

    candles = filter_candles_for_session(payload.candles_by_symbol.get(candidate.symbol) or [], session_mode, payload.now)
    min_candles = min_candle_count(session_mode, payload.now)
    if len(candles) < min_candles:
        return None
    metrics = calculate_metrics(candles, spy_return, candidate)
    if metrics["latestClose"] is None:
        return None
    if session_mode == "regular" and metrics["sessionDollarVolume"] < MIN_SESSION_DOLLAR_VOLUME:
        return None
    if session_mode == "pre" and metrics["sessionDollarVolume"] < PRE_MIN_SESSION_DOLLAR_VOLUME:
        return None
    if session_mode == "regular" and metrics["intradayRangePct"] > profile.max_drawdown_pct * 1.5:
        return None
    max_weight = {"conservative": 15.0, "balanced": 25.0, "aggressive": 35.0}.get(profile.risk_level, 25.0)
    position_weight = portfolio["weights"].get(candidate.symbol, 0.0)
    if position_weight > max_weight:
        return None
    sector_weight = portfolio["sector_weights"].get(candidate.sector, 0.0)
    sector_caps = sector_risk_caps(profile.risk_level)
    if sector_weight >= sector_caps["hard"]:
        return None
    if session_mode == "regular" and profile.risk_level == "conservative" and metrics["return3hPct"] > 5:
        return None

    reasons: list[dict[str, Any]] = []
    risks: list[str] = []
    alpha_score = alpha_points(metrics, reasons, session_mode=session_mode)
    catalyst_score = catalyst_points(
        candidate,
        recent_news_items(payload.news_by_symbol.get(candidate.symbol) or [], now=payload.now),
        metrics,
        reasons,
        session_mode=session_mode,
    )
    execution_score = execution_points(metrics, reasons, risks, session_mode=session_mode)
    risk_penalty = portfolio_risk_penalty(candidate, portfolio, sector_caps, risks)

    if session_mode == "regular" and profile.risk_level == "conservative":
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
    confidence = confidence_for(metrics, reasons, session_mode=session_mode)
    metrics_snapshot = {
        **metrics,
        "changePercent": candidate.change_percent,
        "source": candidate.source,
        "sessionMode": session_mode,
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
        "changePercent": candidate.change_percent,
        **sector_payload_fields(candidate.sector),
        "reasons": reasons[:5],
        "riskWarnings": risks,
        "metricsSnapshot": metrics_snapshot,
    }


def score_candidate_from_market_snapshot(
    candidate: Candidate,
    payload: RecommendationInput,
    portfolio: dict[str, Any],
    excluded_symbols: set[str],
) -> dict[str, Any] | None:
    profile = payload.profile
    session_mode = normalize_session_mode(payload.session_mode)
    excluded_sectors = set(normalize_sector_list(list(profile.excluded_sectors)))
    if candidate.symbol in excluded_symbols or normalize_sector(candidate.sector) in excluded_sectors:
        return None

    sector_weight = portfolio["sector_weights"].get(candidate.sector, 0.0)
    sector_caps = sector_risk_caps(profile.risk_level)
    if sector_weight >= sector_caps["hard"]:
        return None

    reasons: list[dict[str, Any]] = []
    score_breakdown = fallback_score_breakdown(candidate, payload, reasons, session_mode=session_mode)
    score = round(max(0.0, min(100.0, sum(score_breakdown.values()))), 2)
    confidence = fallback_confidence(candidate, reasons)
    candles = filter_candles_for_session(payload.candles_by_symbol.get(candidate.symbol) or [], session_mode, payload.now)
    metrics_snapshot = {
        "latestClose": candidate.last_price,
        "return3hPct": candidate.change_percent or 0.0,
        "relativeStrength": 0.0,
        "volumeRatio": 0.0,
        "breakout": False,
        "intradayRangePct": 0.0,
        "sessionDollarVolume": round(candidate.session_dollar_volume or 0.0, 2),
        "candleCount": len(candles),
        "dataFreshness": "",
        "changePercent": candidate.change_percent,
        "source": candidate.source,
        "sessionMode": session_mode,
        "sectorWeight": round(sector_weight, 6),
        "excludedReason": None,
        "scoreBreakdown": {key: round(value, 4) for key, value in score_breakdown.items()},
        "fallback": True,
        "fallbackReason": "session_candle_snapshot_fill",
    }
    return {
        "symbol": candidate.symbol,
        "action": "buy",
        "rank": 0,
        "score": score,
        "confidence": confidence,
        "changePercent": candidate.change_percent,
        **sector_payload_fields(candidate.sector),
        "reasons": reasons[:5],
        "riskWarnings": [],
        "metricsSnapshot": metrics_snapshot,
    }


def fallback_score_breakdown(
    candidate: Candidate,
    payload: RecommendationInput,
    reasons: list[dict[str, Any]],
    *,
    session_mode: str,
) -> dict[str, float]:
    change = candidate.change_percent
    liquidity = candidate.session_dollar_volume or 0.0
    momentum = 0.0
    liquidity_score = 0.0
    context = 0.0
    catalyst = 0.0
    variant = symbol_variant(candidate.symbol)

    if change is not None:
        if change > 0:
            momentum = min(28.0, change / 4.0 * 28.0)
            reasons.append(reason("market_momentum", positive_momentum_reason(candidate, change, variant), momentum))
        elif change < 0:
            momentum = min(12.0, abs(change) / 4.0 * 12.0)
            reasons.append(reason("market_mover", negative_momentum_reason(candidate, change, variant), momentum))
        else:
            momentum = 4.0
            reasons.append(reason("market_stability", stable_momentum_reason(candidate, variant), momentum))

    if liquidity >= 250_000_000:
        liquidity_score = 22.0
        reasons.append(reason("liquidity", high_liquidity_reason(candidate, liquidity, variant), liquidity_score))
    elif liquidity >= 50_000_000:
        liquidity_score = 16.0
        reasons.append(reason("liquidity", medium_liquidity_reason(candidate, liquidity, variant), liquidity_score))
    elif liquidity >= 10_000_000:
        liquidity_score = 10.0
        reasons.append(reason("liquidity", baseline_liquidity_reason(candidate, liquidity, variant), liquidity_score))
    elif liquidity > 0:
        liquidity_score = 5.0
        reasons.append(reason("liquidity_watch", thin_liquidity_reason(candidate, liquidity, variant), liquidity_score))

    if candidate.source == "related_sector":
        context = 8.0
        reasons.append(reason("sector_context", sector_context_reason(candidate, variant), context))
    else:
        context = 6.0
        reasons.append(reason("market_rank", market_rank_reason(candidate, variant), context))

    news_items = recent_news_items(payload.news_by_symbol.get(candidate.symbol) or [], now=payload.now)
    if news_items:
        catalyst = 8.0 if session_mode == "regular" else 10.0
        reasons.append(reason("catalyst", catalyst_reason(candidate, variant), catalyst))

    if not reasons:
        context = 6.0
        reasons.append(reason("market_snapshot", market_snapshot_reason(candidate, variant), context))

    return {
        "momentum": momentum,
        "liquidity": liquidity_score,
        "context": context,
        "catalyst": catalyst,
    }


def fallback_confidence(candidate: Candidate, reasons: list[dict[str, Any]]) -> float:
    confidence = 0.48
    if candidate.change_percent is not None:
        confidence += 0.08
    if candidate.session_dollar_volume >= 50_000_000:
        confidence += 0.1
    elif candidate.session_dollar_volume > 0:
        confidence += 0.05
    confidence += min(0.12, len(reasons) * 0.03)
    return round(min(0.75, confidence), 4)


def symbol_variant(symbol: str, modulo: int = 4) -> int:
    value = 2_166_136_261
    for char in normalize_symbol(symbol):
        value ^= ord(char)
        value = (value * 16_777_619) & 0xFFFFFFFF
    return value % max(1, modulo)


def positive_momentum_reason(candidate: Candidate, change: float, variant: int) -> str:
    templates = [
        f"오늘 {change:+.2f}% 상승하며 매수세가 먼저 붙은 종목입니다.",
        f"장중 흐름이 {change:+.2f}%로 우상향해 단기 추세 후보로 올렸습니다.",
        f"{candidate.symbol}는 당일 수익률 {change:+.2f}%로 시장 대비 탄력이 보입니다.",
        f"가격이 {change:+.2f}% 올라 momentum 관점에서 상위 후보에 들어왔습니다.",
        f"{candidate.symbol}는 당일 고점권 흐름이 이어져 추세 확인 대상으로 잡았습니다.",
        f"오늘 수익률 {change:+.2f}%가 확인돼 단기 강도 점수를 받았습니다.",
    ]
    return templates[variant % len(templates)]


def negative_momentum_reason(candidate: Candidate, change: float, variant: int) -> str:
    templates = [
        f"오늘 {change:.2f}% 조정 중이라 반등 감시 후보로 분류했습니다.",
        f"{candidate.symbol}는 낙폭이 {change:.2f}%로 커 변동성 기회가 있습니다.",
        f"당일 약세가 뚜렷해 가격 회복 여부를 볼 만한 종목입니다.",
        f"{change:.2f}% 움직임으로 관심이 몰린 하락 변동성 후보입니다.",
    ]
    return templates[variant % len(templates)]


def stable_momentum_reason(candidate: Candidate, variant: int) -> str:
    templates = [
        "가격 변동이 제한적이라 진입 가격 관리가 비교적 쉽습니다.",
        f"{candidate.symbol}는 당일 방향성이 과열되지 않아 관찰 후보로 유지했습니다.",
        "급등락보다 안정적인 흐름을 보여 보수적인 진입 후보입니다.",
        "당일 변동폭이 작아 다음 거래량 변화를 확인하기 좋습니다.",
    ]
    return templates[variant % len(templates)]


def high_liquidity_reason(candidate: Candidate, liquidity: float, variant: int) -> str:
    volume = format_dollar_volume(liquidity)
    templates = [
        f"거래대금이 {volume} 수준으로 커 체결 부담이 낮습니다.",
        f"{candidate.symbol}는 유동성이 두꺼워 빠른 진입/청산이 유리합니다.",
        f"세션 거래가 {volume}까지 쌓여 기관성 수급 확인에 적합합니다.",
        "대형 거래대금이 받쳐줘 추천 리스트 상단에 둘 근거가 있습니다.",
        f"{volume} 규모의 거래가 붙어 실제 매매 가능한 후보로 봤습니다.",
        f"{candidate.symbol}는 거래 회전이 충분해 발표용 추천 리스트에 적합합니다.",
    ]
    return templates[variant % len(templates)]


def medium_liquidity_reason(candidate: Candidate, liquidity: float, variant: int) -> str:
    volume = format_dollar_volume(liquidity)
    templates = [
        f"거래대금 {volume}로 기본 체결 여건을 충족합니다.",
        f"{candidate.symbol}는 후보군 내 유동성이 충분한 편입니다.",
        "매수 후보로 보기 위한 최소 거래 활력이 확인됩니다.",
        f"세션 거래가 {volume} 수준이라 가격 왜곡 부담이 크지 않습니다.",
    ]
    return templates[variant % len(templates)]


def baseline_liquidity_reason(candidate: Candidate, liquidity: float, variant: int) -> str:
    volume = format_dollar_volume(liquidity)
    templates = [
        f"거래대금 {volume}가 관측돼 보조 후보로 포함했습니다.",
        "유동성은 중간 수준이지만 시장 스냅샷 기준을 통과했습니다.",
        f"{candidate.symbol}는 기본 거래량이 있어 관찰 리스트에 남겼습니다.",
        "체결 규모를 작게 잡으면 추적 가능한 유동성입니다.",
    ]
    return templates[variant % len(templates)]


def thin_liquidity_reason(candidate: Candidate, liquidity: float, variant: int) -> str:
    volume = format_dollar_volume(liquidity)
    templates = [
        f"거래대금은 {volume}로 얇지만 움직임이 관측돼 감시 후보입니다.",
        "유동성은 제한적이라 소액 접근 전제로만 후보에 포함했습니다.",
        f"{candidate.symbol}는 체결 부담을 감안해야 하는 보조 후보입니다.",
        "거래가 얇아 추격보다 대기 주문 관점에서 볼 종목입니다.",
    ]
    return templates[variant % len(templates)]


def sector_context_reason(candidate: Candidate, variant: int) -> str:
    sector = sector_label_ko(candidate.sector)
    templates = [
        f"{sector} 섹터 노출을 보강할 수 있는 관련 후보입니다.",
        f"현재 관심 섹터와 맞물려 {sector} 안에서 선별했습니다.",
        f"{sector} 흐름을 같이 볼 때 포트폴리오 확장 후보입니다.",
        f"섹터 분산 관점에서 {sector} 쪽 대안으로 올렸습니다.",
    ]
    return templates[variant % len(templates)]


def market_rank_reason(candidate: Candidate, variant: int) -> str:
    templates = [
        "S&P 500 heatmap에서 유동성/변동성 상위권으로 잡힌 후보입니다.",
        "시장 전체 스냅샷에서 거래와 가격 움직임이 함께 포착됐습니다.",
        f"{candidate.symbol}는 당일 market rank 기준으로 우선순위가 높습니다.",
        "섹터와 무관하게 시장 강도 기준으로 리스트에 들어왔습니다.",
    ]
    return templates[variant % len(templates)]


def catalyst_reason(candidate: Candidate, variant: int) -> str:
    templates = [
        "최근 7일 뉴스가 있어 가격 움직임을 설명할 재료가 있습니다.",
        f"{candidate.symbol} 관련 최신 이벤트가 수급 판단 근거를 보탭니다.",
        "뉴스 모멘텀이 확인돼 단순 가격 움직임보다 설명력이 있습니다.",
        "최근 기사 흐름이 관찰돼 catalyst 점수를 더했습니다.",
    ]
    return templates[variant % len(templates)]


def market_snapshot_reason(candidate: Candidate, variant: int) -> str:
    templates = [
        "시장 스냅샷 기준으로 15개 추천을 채우기 위해 포함했습니다.",
        f"{candidate.symbol}는 보조 점수 기준으로 컷오프 안에 들어왔습니다.",
        "세션 데이터가 부족해도 heatmap 기준 우선순위가 남아 있습니다.",
        "정규 scoring 후보가 부족해 시장 순위 기반으로 보강했습니다.",
    ]
    return templates[variant % len(templates)]


def format_dollar_volume(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


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


def alpha_points(metrics: dict[str, Any], reasons: list[dict[str, Any]], *, session_mode: str) -> float:
    score = 0.0
    ret = float(metrics["return3hPct"])
    rel = float(metrics["relativeStrength"])
    vol = float(metrics["volumeRatio"])
    momentum_label = "장전/데이장 수익률" if session_mode == "pre" else "최근 3시간 수익률"
    if ret > 0:
        pts = min(18.0, ret / 4.0 * 18.0)
        score += pts
        reasons.append(reason("momentum", f"{momentum_label}이 {ret:.2f}%로 양호합니다.", pts))
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
        reasons.append(reason("breakout", "현재 세션 전고점 돌파 신호가 있습니다.", 12.0))
    return score


def catalyst_points(
    candidate: Candidate,
    news_items: list[dict[str, Any]],
    metrics: dict[str, Any],
    reasons: list[dict[str, Any]],
    *,
    session_mode: str,
) -> float:
    if not news_items:
        return 0.0
    score = 12.0 if session_mode == "pre" else 8.0
    reasons.append(reason("catalyst", "최근 7일 뉴스 또는 이벤트 근거가 있습니다.", score))
    if metrics["volumeRatio"] >= 1.5:
        bonus = 6.0 if session_mode == "pre" else 5.0
        score += bonus
        reasons.append(reason("catalyst_volume", "뉴스와 거래량 증가가 함께 관측됩니다.", bonus))
    negative = any(str(item.get("sentiment") or item.get("impactDirection") or "").lower() in {"negative", "bearish"} for item in news_items)
    if not negative:
        score += 3 if session_mode == "pre" else 2
    else:
        score -= 5
    return max(0.0, score)


def execution_points(metrics: dict[str, Any], reasons: list[dict[str, Any]], risks: list[str], *, session_mode: str) -> float:
    score = 0.0
    if metrics["intradayRangePct"] <= 4:
        score += 8
        reasons.append(reason("execution_risk", "장중 변동폭이 과도하지 않아 진입 가격 관리가 가능합니다.", 8))
    else:
        risks.append("장중 변동성이 커 매수 가격 관리가 필요합니다.")
    strong_liquidity = PRE_MIN_SESSION_DOLLAR_VOLUME * 10 if session_mode == "pre" else MIN_SESSION_DOLLAR_VOLUME * 5
    if metrics["sessionDollarVolume"] >= strong_liquidity:
        score += 7
        reasons.append(reason("liquidity", "세션 거래대금이 충분해 체결 부담이 낮습니다.", 7))
    elif session_mode == "pre":
        risks.append("장전/데이장 거래대금이 제한적이라 체결 가격 관리가 필요합니다.")
    if metrics["dataFreshness"]:
        score += 5
    if metrics["return3hPct"] <= 6:
        score += 5
    else:
        risks.append("단기 상승 폭이 커 단기 되돌림 리스크가 있습니다.")
    return score


def confidence_for(metrics: dict[str, Any], reasons: list[dict[str, Any]], *, session_mode: str) -> float:
    confidence = 0.45
    if session_mode == "pre":
        confidence += min(0.2, max(0, metrics["candleCount"] - PRE_MIN_CANDLE_COUNT) / 60 * 0.2)
        confidence += 0.13 if metrics["sessionDollarVolume"] >= PRE_MIN_SESSION_DOLLAR_VOLUME * 10 else 0.08
    else:
        confidence += min(0.2, max(0, metrics["candleCount"] - 60) / 180 * 0.2)
        confidence += 0.15 if metrics["sessionDollarVolume"] >= MIN_SESSION_DOLLAR_VOLUME * 5 else 0.08
    confidence += min(0.2, len(reasons) * 0.04)
    return round(min(0.95, confidence), 4)


def min_candle_count(session_mode: str, now: datetime) -> int:
    if normalize_session_mode(session_mode) == "pre":
        return PRE_MIN_CANDLE_COUNT
    local = now.astimezone(MARKET_TZ)
    if time(9, 30) <= local.time() < time(10, 30):
        return REGULAR_OPENING_MIN_CANDLE_COUNT
    return MIN_CANDLE_COUNT


def recommendation_cutoffs(session_mode: str, metrics: dict[str, Any]) -> dict[str, float]:
    if normalize_session_mode(session_mode) == "pre":
        return {"score": 60.0, "confidence": 0.5}
    if int(metrics.get("candleCount") or 0) < MIN_CANDLE_COUNT:
        return {"score": 65.0, "confidence": 0.55}
    return {"score": 75.0, "confidence": 0.7}


def filter_candles_for_session(candles: list[dict[str, Any]], session_mode: str, now: datetime) -> list[dict[str, Any]]:
    normalized_mode = normalize_session_mode(session_mode)
    target_date = now.astimezone(MARKET_TZ).date()
    filtered: list[dict[str, Any]] = []
    for candle in candles:
        timestamp = parse_datetime(candle.get("timestamp") or candle.get("eventTime") or candle.get("updatedAt"))
        if timestamp is None:
            continue
        if timestamp > now:
            continue
        local = timestamp.astimezone(MARKET_TZ)
        if local.date() != target_date:
            continue
        if normalized_mode == "pre" and time(4, 0) <= local.time() < time(9, 30):
            filtered.append(candle)
        elif normalized_mode == "regular" and time(9, 30) <= local.time() < time(16, 0):
            filtered.append(candle)
    return filtered


def recent_news_items(news_items: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=NEWS_LOOKBACK_DAYS)
    rows = []
    for item in news_items:
        observed = parse_datetime(
            item.get("publishedAt")
            or item.get("published_at")
            or item.get("localizedAt")
            or item.get("receivedAt")
        )
        if observed is None or observed < cutoff:
            continue
        rows.append(item)
    return rows


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def portfolio_summary(positions: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, float] = {}
    sector_values: dict[str, float] = {}
    total = 0.0
    cash = 0.0
    for position in positions:
        symbol = normalize_symbol(position.get("symbol"))
        value = as_float(position.get("marketValueKrw")) or as_float(position.get("marketValueForeign")) or 0.0
        sector = normalize_sector(position.get("sector"))
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
    risks.append(f"현재 포트폴리오의 {sector_label_ko(candidate.sector)} 섹터 비중이 {sector_weight:.1f}%로 높아 신규 비중은 제한해야 합니다.")
    return penalty


def enforce_sector_diversity(items: list[dict[str, Any]], *, max_items: int, max_per_sector: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in items:
        sector = normalize_sector(item.get("sector"))
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
        recommendation_style=str(row.get("recommendation_style") or row.get("recommendationStyle") or "balanced"),
        horizon=str(row.get("horizon") or "intraday"),
        max_drawdown_pct=float(row.get("max_drawdown_pct") or row.get("maxDrawdownPct") or 5),
        preferred_sectors=tuple(normalize_sector_list(row.get("preferred_sectors") or row.get("preferredSectors") or [])),
        excluded_sectors=tuple(normalize_sector_list(row.get("excluded_sectors") or row.get("excludedSectors") or [])),
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
