from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

from app.core.sectors import normalize_sector

from .professional import parse_datetime, position_value
from .professional_v2 import RISK_PRESETS


DECISION_VERSION = "recommendation-decision.v1"
RISK_PER_TRADE = {"conservative": 0.0025, "balanced": 0.005, "aggressive": 0.01}
DIRECT_SPREAD_CAP_BPS = {"conservative": 5.0, "balanced": 10.0, "aggressive": 20.0}
MATERIAL_PENALTIES = {
    "overextension",
    "volatilityMismatch",
    "weakConfirmation",
    "concentrationProximity",
}
EVIDENCE_BLOCK_ORDER = (
    "trendStrength",
    "participationConfirmation",
    "priceStructure",
    "catalystQuality",
    "executionQuality",
    "qualityStability",
)
SOFT_PENALTY_CAUTIONS = {
    "overextension": ("가격 과열", "현재 가격이 VWAP에서 과도하게 이격돼 추격 진입 위험이 큽니다."),
    "volatilityMismatch": ("변동성", "최근 변동성이 현재 위험성향의 안정성 기준보다 높습니다."),
    "weakConfirmation": ("근거 일치도", "가격 흐름과 거래 참여가 같은 방향을 가리키지 않아 추가 확인이 필요합니다."),
    "limitedPortfolioEvidence": ("포트폴리오 근거", "최신 포트폴리오 근거가 부족해 계좌 적합도를 충분히 반영하지 못했습니다."),
    "concentrationProximity": ("집중도", "진입 후 섹터 집중도가 현재 위험성향의 허용 한도에 가까워질 수 있습니다."),
}
RISK_WARNING_CAUTIONS = {
    "호가 스프레드가 현재 위험성향의 실행 한도에 근접합니다.": (
        "spread_proximity",
        "체결 여건",
    ),
    "중앙값 거래대금이 현재 위험성향의 유동성 하한에 근접합니다.": (
        "liquidity_proximity",
        "유동성",
    ),
}
ACTION_LABELS = {
    "buy": "매수 추천",
    "conditional_buy": "조건부 매수",
    "watch": "관찰",
    "not_suitable": "추천 제외",
}
COUNTER_EVIDENCE_PRIORITY = {
    code: index
    for index, code in enumerate((
        "missing_required_price_evidence",
        "position_size_below_one_share",
        "current_relative_strength",
        "last60_relative_strength",
        "volume_confirmation",
        "vwap_hold",
        "overextension",
        "quoted_spread",
        "material_penalty",
        "evidence_reliability",
        "base_score",
        "personal_score",
    ))
}


def enrich_direct_recommendations(
    items: list[dict[str, Any]],
    *,
    risk_level: str,
    portfolio_snapshot: dict[str, Any] | None,
    target_session_date: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for source in items:
        item = deepcopy(source)
        decision = build_decision(
            item,
            risk_level=risk_level,
            target_session_date=target_session_date,
        )
        sizing = build_sizing(
            item,
            decision=decision,
            risk_level=risk_level,
            portfolio_snapshot=portfolio_snapshot,
            cutoff=cutoff,
        )
        action = str(decision["action"])
        if action in {"buy", "conditional_buy"} and sizing.get("status") == "blocked":
            action = "not_suitable"
            decision["action"] = action
            decision["label"] = ACTION_LABELS[action]
            decision["failedConditions"] = [
                *decision.get("failedConditions", []),
                {
                    "code": "position_size_below_one_share",
                    "label": "추천 가능 수량",
                    "actual": 0,
                    "required": "1주 이상",
                },
            ]
        item["action"] = action
        item["decision"] = decision
        item["sizing"] = sizing
        item["keyEvidence"] = build_key_evidence(item)
        item["counterEvidence"] = build_counter_evidence(item, decision)
        item["riskWarnings"] = clean_risk_warnings(item.get("riskWarnings") or [])
        item["cautions"] = build_cautions(item, decision)
        item["explanation"] = decision_explanation(item, target_session_date=target_session_date)
        enriched.append(item)
    return enriched


def build_decision(
    item: dict[str, Any],
    *,
    risk_level: str,
    target_session_date: str,
) -> dict[str, Any]:
    metrics = _record(item.get("metricsSnapshot"))
    raw = _record(metrics.get("rawFactors"))
    penalties = _record(metrics.get("softPenalties"))
    base_score = _number(metrics.get("adjustedSetupScore"), _number(metrics.get("baseSetupScore"), item.get("score")))
    personal_score = _number(metrics.get("personalScore"), item.get("score"))
    reliability = _number(metrics.get("evidenceReliability"), _number(item.get("confidence")) * 100.0)
    spread_cap = DIRECT_SPREAD_CAP_BPS.get(risk_level, DIRECT_SPREAD_CAP_BPS["balanced"])

    required = {
        "latestClose": _positive(raw.get("latestClose")),
        "atr": _positive(raw.get("atr")),
        "vwap": _positive(raw.get("vwap")),
        "sessionHigh": _positive(raw.get("sessionHigh")),
        "last60MinuteLow": _positive(raw.get("last60MinuteLow")),
        "quoteBid": _positive(raw.get("quoteBid")),
        "quoteAsk": _positive(raw.get("quoteAsk")),
    }
    missing_required = [key for key, value in required.items() if value <= 0]
    if required["quoteAsk"] and required["quoteBid"] and required["quoteAsk"] < required["quoteBid"]:
        missing_required.append("validQuote")

    conditions = [
        _condition("base_score", "V3 기본 점수", base_score, 65.0, base_score >= 65.0),
        _condition("personal_score", "개인화 점수", personal_score, 65.0, personal_score >= 65.0),
        _condition("evidence_reliability", "근거 신뢰도", reliability, 75.0, reliability >= 75.0),
        _condition(
            "current_relative_strength",
            "SPY 대비 당일 상대강도",
            _number(raw.get("currentSessionRelativeStrength")),
            "> 0%p",
            _number(raw.get("currentSessionRelativeStrength")) > 0,
        ),
        _condition(
            "last60_relative_strength",
            "마감 전 60분 상대강도",
            _number(raw.get("last60MinuteRelativeStrength")),
            ">= 0%p",
            _number(raw.get("last60MinuteRelativeStrength")) >= 0,
        ),
        _condition(
            "volume_confirmation",
            "동시간대 거래량",
            _number(raw.get("clockAdjustedVolumeRatio")),
            ">= 1.0배",
            _number(raw.get("clockAdjustedVolumeRatio")) >= 1.0,
        ),
        _condition(
            "vwap_hold",
            "종가와 VWAP",
            required["latestClose"] - required["vwap"],
            ">= 0",
            bool(required["latestClose"] and required["vwap"] and required["latestClose"] >= required["vwap"]),
        ),
        _condition(
            "overextension",
            "VWAP 이격도",
            _number(raw.get("overextensionAtr"), math.inf),
            "<= 1.5 ATR",
            _number(raw.get("overextensionAtr"), math.inf) <= 1.5,
        ),
        _condition(
            "quoted_spread",
            "호가 스프레드",
            _number(raw.get("quotedSpreadBps"), math.inf),
            f"<= {spread_cap:g}bp",
            _number(raw.get("quotedSpreadBps"), math.inf) <= spread_cap,
        ),
        _condition(
            "material_penalty",
            "중대 위험 경고",
            sorted(key for key in penalties if key in MATERIAL_PENALTIES),
            "없음",
            not any(key in MATERIAL_PENALTIES for key in penalties),
        ),
    ]
    failed = [condition for condition in conditions if not condition.pop("passed")]
    confirmation_failures = [
        condition
        for condition in failed
        if condition["code"] not in {"base_score", "personal_score", "evidence_reliability"}
    ]
    all_required = not missing_required
    direct = all_required and not failed
    conditional = (
        all_required
        and not direct
        and base_score >= 60.0
        and personal_score >= 60.0
        and reliability >= 70.0
        and _number(raw.get("overextensionAtr"), math.inf) <= 2.0
        and len(confirmation_failures) <= 1
    )
    action = "buy" if direct else "conditional_buy" if conditional else "watch"
    levels = build_entry_levels(raw) if action in {"buy", "conditional_buy"} and all_required else None
    if levels is None and action in {"buy", "conditional_buy"}:
        action = "watch"
    if missing_required:
        failed.insert(0, {
            "code": "missing_required_price_evidence",
            "label": "필수 가격 근거",
            "actual": missing_required,
            "required": "종가·ATR·VWAP·세션 고가·최근 60분 저가·유효 호가",
        })
    return {
        "version": DECISION_VERSION,
        "action": action,
        "label": ACTION_LABELS[action],
        "riskLevel": risk_level,
        "holdingHorizon": "intraday",
        "entryRoutes": levels["entryRoutes"] if levels else [],
        "invalidationPrice": levels["invalidationPrice"] if levels else None,
        "targetPriceByRoute": levels["targetPriceByRoute"] if levels else {},
        "forceExitAt": f"{target_session_date}T15:50:00-04:00",
        "failedConditions": failed,
    }


def build_entry_levels(raw: dict[str, Any]) -> dict[str, Any] | None:
    close = _positive(raw.get("latestClose"))
    atr = _positive(raw.get("atr"))
    vwap = _positive(raw.get("vwap"))
    session_high = _positive(raw.get("sessionHigh"))
    last60_low = _positive(raw.get("last60MinuteLow"))
    tick = _positive(raw.get("tickSize")) or 0.01
    if not all((close, atr, vwap, session_high, last60_low)):
        return None
    raw_support_stop = min(vwap, last60_low) - 0.10 * atr
    stop_distance = min(1.0 * atr, max(0.5 * atr, close - raw_support_stop))
    stop = _price(close - stop_distance, tick, ROUND_FLOOR)
    pullback_low = _price(max(vwap, close - 0.25 * atr), tick, ROUND_HALF_UP)
    pullback_high = _price(close + 0.10 * atr, tick, ROUND_CEILING)
    breakout_trigger = _price(session_high + tick, tick, ROUND_CEILING)
    chase_limit = _price(breakout_trigger + 0.15 * atr, tick, ROUND_CEILING)
    routes: list[dict[str, Any]] = []
    targets: dict[str, float] = {}
    if pullback_low <= pullback_high and pullback_high > stop:
        routes.append({"type": "pullback", "entryLow": pullback_low, "entryHigh": pullback_high})
        targets["pullback"] = _price(pullback_high + 1.5 * (pullback_high - stop), tick, ROUND_CEILING)
    if breakout_trigger <= chase_limit and chase_limit > stop:
        routes.append({"type": "breakout", "trigger": breakout_trigger, "chaseLimit": chase_limit})
        targets["breakout"] = _price(chase_limit + 1.5 * (chase_limit - stop), tick, ROUND_CEILING)
    if not routes:
        return None
    return {"entryRoutes": routes, "invalidationPrice": stop, "targetPriceByRoute": targets}


def build_sizing(
    item: dict[str, Any],
    *,
    decision: dict[str, Any],
    risk_level: str,
    portfolio_snapshot: dict[str, Any] | None,
    cutoff: datetime,
) -> dict[str, Any]:
    risk_pct = RISK_PER_TRADE.get(risk_level, RISK_PER_TRADE["balanced"])
    if decision.get("action") not in {"buy", "conditional_buy"}:
        return {"status": "not_applicable", "riskBudgetPct": round(risk_pct * 100.0, 4)}
    payload = _portfolio_payload(portfolio_snapshot)
    observed = parse_datetime(
        (portfolio_snapshot or {}).get("source_as_of")
        or payload.get("sourceAsOf")
        or payload.get("asOf")
    )
    if not payload or observed is None or observed > cutoff or cutoff - observed > timedelta(hours=24):
        return {
            "status": "unavailable",
            "riskBudgetPct": round(risk_pct * 100.0, 4),
            "recommendedShares": None,
            "estimatedNotional": None,
            "capReasons": ["portfolio_snapshot_unavailable"],
        }
    equity = _portfolio_equity(payload)
    account = _record(payload.get("account"))
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cash") or account.get("cashForeign"))
    if equity <= 0:
        return {
            "status": "unavailable",
            "riskBudgetPct": round(risk_pct * 100.0, 4),
            "recommendedShares": None,
            "estimatedNotional": None,
            "capReasons": ["portfolio_equity_unavailable"],
        }
    routes = decision.get("entryRoutes") or []
    worst_entry = max(
        [_positive(route.get("entryHigh") or route.get("chaseLimit")) for route in routes],
        default=0.0,
    )
    stop = _positive(decision.get("invalidationPrice"))
    risk_per_share = worst_entry - stop
    if worst_entry <= 0 or risk_per_share <= 0:
        return {"status": "blocked", "riskBudgetPct": round(risk_pct * 100.0, 4), "recommendedShares": 0, "estimatedNotional": 0.0, "capReasons": ["invalid_entry_risk"]}
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    symbol = str(item.get("symbol") or "").upper()
    sector = normalize_sector(item.get("sector"))
    symbol_value = sum(position_value(row) for row in positions if str(row.get("symbol") or "").upper() == symbol)
    sector_value = sum(position_value(row) for row in positions if normalize_sector(row.get("sector")) == sector)
    preset = RISK_PRESETS.get(risk_level, RISK_PRESETS["balanced"])
    limits = {
        "risk_budget": math.floor(equity * risk_pct / risk_per_share),
        "position_cap_5pct": math.floor(equity * 0.05 / worst_entry),
        "cash": math.floor(cash / worst_entry),
        "single_stock_cap": math.floor(max(0.0, equity * preset["maxSingleStockPct"] / 100.0 - symbol_value) / worst_entry),
        "sector_cap": math.floor(max(0.0, equity * preset["maxSectorPct"] / 100.0 - sector_value) / worst_entry),
    }
    quantity = max(0, min(limits.values()))
    cap_reasons = sorted(key for key, value in limits.items() if value == quantity)
    return {
        "status": "ready" if quantity >= 1 else "blocked",
        "riskBudgetPct": round(risk_pct * 100.0, 4),
        "riskBudgetAmount": round(equity * risk_pct, 2),
        "recommendedShares": quantity,
        "estimatedNotional": round(quantity * worst_entry, 2),
        "worstAllowedEntry": round(worst_entry, 2),
        "riskPerShare": round(risk_per_share, 2),
        "capReasons": cap_reasons,
    }


def build_key_evidence(item: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = _record(item.get("metricsSnapshot"))
    raw = _record(metrics.get("rawFactors"))
    current_rs = _number(raw.get("currentSessionRelativeStrength"))
    last60_rs = _number(raw.get("last60MinuteRelativeStrength"))
    volume = _number(raw.get("clockAdjustedVolumeRatio"))
    close = _number(raw.get("latestClose"))
    vwap = _number(raw.get("vwap"))
    spread = _number(raw.get("quotedSpreadBps"))
    atr = _number(raw.get("atr"))
    action = str(item.get("action") or "watch")
    risk_level = str(_record(item.get("decision")).get("riskLevel") or "balanced")
    risk_label = {"conservative": "보수형", "balanced": "균형형", "aggressive": "적극형"}.get(
        risk_level, "균형형"
    )
    spread_cap = DIRECT_SPREAD_CAP_BPS.get(risk_level, DIRECT_SPREAD_CAP_BPS["balanced"])
    liquidity_floor = float(
        RISK_PRESETS.get(risk_level, RISK_PRESETS["balanced"])["minimumMedianDollarVolume"]
    )
    close_vwap_gap_pct = (close / vwap - 1.0) * 100.0 if close > 0 and vwap > 0 else 0.0
    if current_rs > 0 and last60_rs >= 0:
        strength_assessment = "strong"
        strength_interpretation = (
            "시장 전체 상승에 편승한 움직임인지 구분하기 위해 SPY를 기준으로 비교했습니다. "
            "장중과 마감 구간이 같은 방향이라 일시적인 초반 급등보다 지속된 종목 수요로 해석했습니다."
        )
    elif current_rs > 0:
        strength_assessment = "mixed"
        strength_interpretation = (
            "시장 전체 움직임과 분리해 보기 위해 SPY를 기준으로 비교했습니다. "
            "장중 강세와 달리 마감 구간이 약해 종목 수요가 하루 끝까지 이어졌다고 보기는 어렵습니다."
        )
    elif last60_rs >= 0:
        strength_assessment = "mixed"
        strength_interpretation = (
            "시장 전체 움직임과 분리해 보기 위해 SPY를 기준으로 비교했습니다. "
            "마감 구간의 회복은 확인됐지만 전체 세션의 약세를 뒤집지는 못해 보조 근거로만 사용했습니다."
        )
    else:
        strength_assessment = "weak"
        strength_interpretation = (
            "시장 전체 움직임과 분리해 보기 위해 SPY를 기준으로 비교했습니다. "
            "장중과 마감 구간 모두 종목 고유의 수요 우위를 확인하지 못해 매수 근거로 사용하지 않았습니다."
        )
    if close < vwap:
        execution_interpretation = (
            "VWAP는 당일 거래량을 반영한 평균 체결가입니다. 종가가 그 아래에 있으면 "
            "장중 평균 매수자의 손익이 불리해 가격 지지와 진입 기준을 세우기 어렵습니다."
        )
    elif action in {"buy", "conditional_buy"}:
        execution_interpretation = (
            "VWAP는 당일 거래량을 반영한 평균 체결가입니다. 종가가 그 위에 있으면 "
            "장중 평균 매수자의 손익이 상대적으로 안정적이어서 눌림 진입과 무효화 기준을 세우기 쉽습니다."
        )
    elif action == "not_suitable":
        execution_interpretation = (
            "VWAP 위 마감은 가격 구조를 판단하는 데 유효하지만 계좌 위험 한도를 대신할 수는 없습니다. "
            "가격 구조와 별도로 현재 계좌에서 감당할 수 있는 수량을 우선 적용했습니다."
        )
    else:
        execution_interpretation = (
            "VWAP 위 마감은 장중 평균 체결가보다 가격이 유지됐다는 뜻이지만, "
            "상대강도와 거래 참여가 함께 확인되지 않으면 단독 매수 근거로 사용하지 않습니다."
        )
    evidence = [
        {
            "code": "market_strength",
            "label": "시장 흐름",
            "primaryValue": f"SPY 대비 {current_rs:+.2f}%p",
            "secondaryValue": f"마감 전 60분 {last60_rs:+.2f}%p",
            "assessment": strength_assessment,
            "interpretation": strength_interpretation,
            "metrics": [
                _evidence_metric(
                    label="당일 상대강도",
                    value_text=f"{current_rs:+.2f}%p",
                    comparison="SPY 대비 · 중립 0%p",
                    value=current_rs,
                    reference=0.0,
                    scale_min=-3.0,
                    scale_max=3.0,
                    tone="positive" if current_rs > 0 else "negative",
                ),
                _evidence_metric(
                    label="마감 전 60분",
                    value_text=f"{last60_rs:+.2f}%p",
                    comparison="SPY 대비 · 중립 0%p",
                    value=last60_rs,
                    reference=0.0,
                    scale_min=-3.0,
                    scale_max=3.0,
                    tone="positive" if last60_rs >= 0 else "negative",
                ),
            ],
        },
        {
            "code": "participation",
            "label": "거래 참여",
            "primaryValue": f"직전 정규장 동시간 대비 {volume:.2f}배",
            "secondaryValue": "확대 기준 1.00배",
            "assessment": "strong" if volume >= 1.0 else "weak",
            "interpretation": (
                "장중 거래량은 개장과 마감에 몰리는 특성이 있어 직전 정규장의 같은 시각과 비교했습니다. "
                + (
                    "평소보다 넓은 시장 참여가 가격 움직임에 동반됐는지 확인하는 근거로 사용했습니다."
                    if volume >= 1.0 else
                    "평소보다 넓은 시장 참여를 확인하지 못해 가격 움직임을 단독 매수 근거로 사용하지 않았습니다."
                )
            ),
            "metrics": [
                _evidence_metric(
                    label="동시간 거래량",
                    value_text=f"{volume:.2f}배",
                    comparison="직전 정규장 동시간 · 기준 1.00배",
                    value=volume,
                    reference=1.0,
                    scale_min=0.0,
                    scale_max=max(2.0, volume * 1.15),
                    tone="positive" if volume >= 1.0 else "negative",
                ),
            ],
        },
        {
            "code": "execution_structure",
            "label": "가격 구조",
            "primaryValue": f"종가 ${close:.2f} · VWAP ${vwap:.2f}",
            "secondaryValue": f"스프레드 {spread:.2f}bp · ATR ${atr:.2f}",
            "assessment": "strong" if close >= vwap else "mixed",
            "interpretation": execution_interpretation,
            "metrics": [
                _evidence_metric(
                    label="종가-VWAP 이격",
                    value_text=f"{close_vwap_gap_pct:+.2f}%",
                    comparison=f"종가 ${close:.2f} · VWAP ${vwap:.2f}",
                    value=close_vwap_gap_pct,
                    reference=0.0,
                    scale_min=-3.0,
                    scale_max=3.0,
                    tone="positive" if close_vwap_gap_pct >= 0 else "negative",
                ),
            ],
        },
    ]
    available = {
        str(value)
        for value in metrics.get("availableBlocks") or ()
        if str(value) in EVIDENCE_BLOCK_ORDER
    }
    scores = _record(metrics.get("blockScores"))
    if "catalystQuality" in available:
        catalyst_score = _number(scores.get("catalystQuality"), 50.0)
        evidence.append(_block_evidence(
            code="catalyst_quality",
            label="뉴스·촉매",
            score=catalyst_score,
            interpretation=(
                "판단 시점 전에 공개된 뉴스만 사용하고 관련성·방향·신선도를 함께 평가했습니다. "
                "가격과 거래량에서 독립된 사건 근거라 시장 움직임의 배경을 교차 확인하는 데 사용했습니다."
            ),
        ))
    if "executionQuality" in available:
        execution_score = _number(scores.get("executionQuality"), 50.0)
        median_dollar_volume = _number(raw.get("medianDollarVolume"))
        execution_metrics = [
            _evidence_metric(
                label="호가 스프레드",
                value_text=f"{spread:.2f}bp",
                comparison=f"{risk_label} 허용 상한 {spread_cap:.1f}bp",
                value=spread,
                reference=spread_cap,
                scale_min=0.0,
                scale_max=max(spread_cap * 1.25, spread * 1.1, 1.0),
                tone="positive" if spread <= spread_cap else "negative",
            ),
        ]
        if median_dollar_volume > 0:
            execution_metrics.append(_evidence_metric(
                label="20일 중앙 거래대금",
                value_text=_format_usd_compact(median_dollar_volume),
                comparison=f"최소 기준 {_format_usd_compact(liquidity_floor)}",
                value=median_dollar_volume,
                reference=liquidity_floor,
                scale_min=0.0,
                scale_max=max(liquidity_floor * 2.0, median_dollar_volume * 1.1),
                tone="positive" if median_dollar_volume >= liquidity_floor else "negative",
            ))
        evidence.append(_block_evidence(
            code="execution_quality",
            label="체결 여건",
            score=execution_score,
            interpretation=(
                "가격 신호가 좋아도 호가가 넓거나 거래대금이 부족하면 실제 체결 비용이 예상 위험을 키울 수 있습니다. "
                "그래서 호가 상한과 최근 중앙 거래대금을 함께 통과한 경우에만 실행 가능한 근거로 사용했습니다."
                if median_dollar_volume > 0 else
                "호가 간격을 위험성향별 상한과 비교해 실제 체결 비용이 예상 위험을 과도하게 키우지 않는지 확인했습니다."
            ),
            metrics=execution_metrics,
        ))
    if "qualityStability" in available:
        evidence.append(_quality_stability_evidence(raw, scores, risk_level=risk_level))
    return evidence


def _block_evidence(
    *,
    code: str,
    label: str,
    score: float,
    interpretation: str,
    metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if score >= 70.0:
        assessment = "strong"
    elif score >= 45.0:
        assessment = "mixed"
    else:
        assessment = "weak"
    return {
        "code": code,
        "label": label,
        "primaryValue": f"{score:.1f}/100",
        "secondaryValue": "",
        "assessment": assessment,
        "interpretation": interpretation,
        "metrics": metrics or [
            _evidence_metric(
                label="V3 관측 점수",
                value_text=f"{score:.1f}/100",
                comparison="강한 근거 기준 70점",
                value=score,
                reference=70.0,
                scale_min=0.0,
                scale_max=100.0,
                tone="positive" if score >= 70.0 else "neutral" if score >= 45.0 else "negative",
            )
        ],
    }


def _quality_stability_evidence(
    raw: dict[str, Any], scores: dict[str, Any], *, risk_level: str
) -> dict[str, Any]:
    has_fundamentals = bool(raw.get("fundamentalAvailable"))
    has_volatility = any(raw.get(key) is not None for key in ("realizedVolatility", "downsideVolatility"))
    score = _number(scores.get("qualityStability"), 50.0)
    realized = _number(raw.get("realizedVolatility"))
    realized_pct = realized * 100.0
    volatility_limit = {"conservative": 3.5, "balanced": 5.5, "aggressive": 8.0}.get(risk_level, 5.5)
    risk_label = {"conservative": "보수형", "balanced": "균형형", "aggressive": "적극형"}.get(
        risk_level, "균형형"
    )
    metrics: list[dict[str, Any]] = []
    details: list[str] = [
        "최근 변동성과 기업 품질은 짧은 가격 신호가 계좌의 위험성향과 맞는지 확인하는 보완 근거입니다."
    ]
    if has_volatility and realized >= 0:
        metrics.append(_evidence_metric(
            label="60일 일간 변동성",
            value_text=f"{realized_pct:.2f}%",
            comparison=f"{risk_label} 기준 {volatility_limit:.1f}%",
            value=realized_pct,
            reference=volatility_limit,
            scale_min=0.0,
            scale_max=max(volatility_limit * 1.25, realized_pct * 1.1, 1.0),
            tone="positive" if realized_pct <= volatility_limit else "negative",
        ))
    company_quality = raw.get("companyQuality")
    if has_fundamentals and company_quality is not None:
        company_score = _number(company_quality)
        metrics.append(_evidence_metric(
            label="기업 품질",
            value_text=f"{company_score:.1f}/100",
            comparison="관측 펀더멘털 품질 점수",
            value=company_score,
            reference=50.0,
            scale_min=0.0,
            scale_max=100.0,
            tone="positive" if company_score >= 50.0 else "negative",
        ))
        details.append("가격 흐름과 별개인 펀더멘털 품질을 함께 확인해 단기 신호에만 의존하지 않도록 했습니다.")
    return _block_evidence(
        code="quality_stability",
        label="안정성·품질",
        score=score,
        interpretation=" ".join(details),
        metrics=metrics or None,
    )


def _evidence_metric(
    *,
    label: str,
    value_text: str,
    comparison: str,
    value: float,
    reference: float,
    scale_min: float,
    scale_max: float,
    tone: str,
) -> dict[str, Any]:
    span = max(scale_max - scale_min, 1e-9)
    value_position = min(100.0, max(0.0, (value - scale_min) / span * 100.0))
    reference_position = min(100.0, max(0.0, (reference - scale_min) / span * 100.0))
    return {
        "label": label,
        "value": value_text,
        "comparison": comparison,
        "valuePositionPct": round(value_position, 2),
        "referencePositionPct": round(reference_position, 2),
        "tone": tone,
    }


def _format_usd_compact(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def build_cautions(item: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, str]]:
    cautions: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    seen_sentences: set[str] = set()
    metrics = _record(item.get("metricsSnapshot"))
    soft_penalties = _record(metrics.get("softPenalties"))

    def add(code: str, label: str, severity: str, sentence: str) -> None:
        normalized = sentence.strip()
        if not normalized or code in seen_codes or normalized in seen_sentences:
            return
        seen_codes.add(code)
        seen_sentences.add(normalized)
        cautions.append({
            "code": code,
            "label": label,
            "severity": severity,
            "sentence": normalized,
        })

    action = str(decision.get("action") or item.get("action") or "watch")
    invalidation = _positive(decision.get("invalidationPrice"))
    breakout = next(
        (
            route for route in decision.get("entryRoutes") or []
            if isinstance(route, dict) and route.get("type") == "breakout"
        ),
        None,
    )
    if action in {"buy", "conditional_buy"} and breakout and invalidation > 0:
        trigger = _positive(breakout.get("trigger"))
        chase_limit = _positive(breakout.get("chaseLimit"))
        if trigger > 0 and chase_limit >= trigger and chase_limit > invalidation:
            risk_per_share = chase_limit - invalidation
            add(
                "chase_limit",
                "추격 진입 기준",
                "warning",
                f"돌파 매수는 ${chase_limit:.2f}까지만 검토합니다. "
                f"상한에서 무효화 기준 ${invalidation:.2f}까지의 하락 폭은 주당 ${risk_per_share:.2f}입니다.",
            )

    for condition in decision.get("failedConditions") or []:
        code = str(condition.get("code") or "").strip()
        if code == "material_penalty" and any(
            _number(value) > 0 and penalty_code in SOFT_PENALTY_CAUTIONS
            for penalty_code, value in soft_penalties.items()
        ):
            continue
        if code:
            add(code, str(condition.get("label") or "추가 확인"), "warning", counter_evidence_sentence(code))

    for code, value in soft_penalties.items():
        if _number(value) <= 0 or code not in SOFT_PENALTY_CAUTIONS:
            continue
        label, sentence = SOFT_PENALTY_CAUTIONS[code]
        add(str(code), label, "warning", sentence)

    for warning in item.get("riskWarnings") or []:
        sentence = str(warning).strip()
        mapped = RISK_WARNING_CAUTIONS.get(sentence)
        if mapped:
            code, label = mapped
            add(code, label, "warning", sentence)

    return cautions


def build_counter_evidence(item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any] | None:
    failed = decision.get("failedConditions") or []
    if failed:
        first = min(
            failed,
            key=lambda row: COUNTER_EVIDENCE_PRIORITY.get(str(row.get("code") or ""), len(COUNTER_EVIDENCE_PRIORITY)),
        )
        return {
            "code": first.get("code"),
            "label": first.get("label"),
            "actual": first.get("actual"),
            "required": first.get("required"),
            "sentence": counter_evidence_sentence(str(first.get("code") or "")),
        }
    metrics = _record(item.get("metricsSnapshot"))
    penalties = _record(metrics.get("softPenalties"))
    observed = [(key, _number(value)) for key, value in penalties.items() if key in MATERIAL_PENALTIES]
    if not observed:
        return None
    code, value = max(observed, key=lambda row: row[1])
    return {
        "code": code,
        "label": "확인된 반대 근거",
        "actual": value,
        "required": 0,
        "sentence": counter_evidence_sentence(code),
    }


def counter_evidence_sentence(code: str) -> str:
    return {
        "base_score": "종합 판단 점수가 직접 매수 기준에 미치지 못했습니다.",
        "personal_score": "개인화 판단 점수가 직접 매수 기준에 미치지 못했습니다.",
        "evidence_reliability": "사용된 가격·거래 근거의 신뢰도가 직접 매수 기준에 미치지 못했습니다.",
        "current_relative_strength": "당일에는 SPY보다 약해 시장 대비 강도가 부족했습니다.",
        "last60_relative_strength": "마감 전에는 시장 대비 강도가 약해 추가 확인이 필요합니다.",
        "volume_confirmation": "동시간 거래량 배수가 직접 매수 기준 1.00배에 미치지 못했습니다.",
        "vwap_hold": "종가가 VWAP 아래에 있어 가격 구조를 추가로 확인해야 합니다.",
        "overextension": "종가가 VWAP에서 과도하게 이격돼 추격 진입 위험이 큽니다.",
        "quoted_spread": "호가 스프레드가 위험 성향의 허용 범위를 넘어 체결 조건 확인이 필요합니다.",
        "material_penalty": "확인 부족 경고가 남아 있어 추가 검증이 필요합니다.",
        "missing_required_price_evidence": "진입 계획에 필요한 가격 근거가 완전하지 않습니다.",
        "position_size_below_one_share": "현재 계좌 한도에서는 한 주 이상 배정할 수 없습니다.",
    }.get(code, "직접 매수 기준에서 추가 확인이 필요한 조건이 남아 있습니다.")


def decision_explanation(item: dict[str, Any], *, target_session_date: str) -> dict[str, Any]:
    explanation = deepcopy(_record(item.get("explanation")))
    action = str(item.get("action") or "watch")
    label = ACTION_LABELS.get(action, ACTION_LABELS["watch"])
    evidence = item.get("keyEvidence") or []
    interpretations = [
        str(row.get("interpretation") or "").strip()
        for row in evidence
        if isinstance(row, dict) and str(row.get("interpretation") or "").strip()
    ]
    counter = _record(item.get("counterEvidence"))
    counter_sentence = str(counter.get("sentence") or "").strip()
    if action == "buy":
        headline = "시장보다 강한 흐름과 활발한 거래가 이어져, 계획된\u00a0가격대에서 매수를 검토할 수 있습니다."
        body_sentences = []
    elif action == "conditional_buy":
        headline = "시장보다 강한 흐름과 거래\u00a0참여는\u00a0확인됐지만, 남은 조건을 확인한 뒤 매수를 검토해야 합니다."
        body_sentences = [
            "가격 구조와 체결 여건은 진입 계획에 사용할 수 있지만, 유의할 점의 남은 조건이 해소되기 전에는 주문하지 않습니다."
        ]
    elif action == "not_suitable":
        headline = "시장 근거와 별개로 현재 계좌 한도에서는 신규 진입에 적합하지 않습니다."
        body_sentences = [interpretations[0] if interpretations else "", counter_sentence or "현재 계좌의 위험예산·현금·집중도 한도에서 한 주 이상 배정할 수 없습니다."]
    else:
        headline = "현재는 직접 매수 조건이 충분하지 않아 관찰 대상으로 유지합니다."
        body_sentences = interpretations[1:3]
    body = " ".join(dict.fromkeys(sentence for sentence in body_sentences if sentence))
    explanation.update({
        "version": "recommendation-explanation.v1",
        "locale": "ko-KR",
        "decisionLabel": label,
        "primary": {
            "source": "deterministic",
            "status": "ready",
            "headline": headline,
            "body": body,
            "model": None,
            "promptVersion": "recommendation-decision-renderer.ko.v7",
            "generatedAt": explanation.get("primary", {}).get("generatedAt") if isinstance(explanation.get("primary"), dict) else None,
        },
    })
    deterministic = _record(explanation.get("deterministic"))
    deterministic["summary"] = body
    deterministic["evidence"] = []
    deterministic["risks"] = []
    deterministic["dataQuality"] = {
        "sentence": "",
        "evidenceReliability": _number(_record(item.get("metricsSnapshot")).get("evidenceReliability")),
        "confidenceMeaning": "evidence_reliability_not_success_probability",
        "cutoff": _record(item.get("metricsSnapshot")).get("cutoff"),
        "missingFactors": [],
        "stale": False,
    }
    explanation["deterministic"] = deterministic
    explanation.setdefault("provenance", {})
    return explanation


def personalization_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def response_digest(payload: dict[str, Any]) -> str:
    core = {
        "scenarioId": payload.get("scenarioId"),
        "evidencePoolDigest": payload.get("evidencePoolDigest"),
        "personalizationDigest": payload.get("personalizationDigest"),
        "targetSessionDate": payload.get("targetSessionDate"),
        "algorithmVersion": payload.get("algorithmVersion"),
        "items": payload.get("items") or [],
    }
    return hashlib.sha256(_canonical_json(core)).hexdigest()


def clean_risk_warnings(values: list[Any]) -> list[str]:
    return [
        str(value)
        for value in values
        if value and "선택 근거가 없어 중립값" not in str(value) and "missingOptional" not in str(value)
    ]


def _condition(code: str, label: str, actual: Any, required: Any, passed: bool) -> dict[str, Any]:
    return {"code": code, "label": label, "actual": actual, "required": required, "passed": passed}


def _portfolio_payload(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    return _record(snapshot.get("payload")) or _record(snapshot)


def _portfolio_equity(payload: dict[str, Any]) -> float:
    account = _record(payload.get("account"))
    explicit = _positive(
        payload.get("totalValue")
        or payload.get("totalEvaluationAmount")
        or account.get("totalValueForeign")
        or account.get("totalValue")
    )
    if explicit:
        return explicit
    positions = [row for row in payload.get("positions", []) if isinstance(row, dict)]
    cash = _positive(payload.get("cash") or payload.get("cashForeign") or account.get("cashForeign") or account.get("cash"))
    return sum(position_value(row) for row in positions) + cash


def _price(value: float, tick: float, rounding: str) -> float:
    quantum = Decimal(str(tick))
    units = (Decimal(str(value)) / quantum).to_integral_value(rounding=rounding)
    return float((units * quantum).quantize(quantum))


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _positive(value: Any) -> float:
    return max(0.0, _number(value))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode()
