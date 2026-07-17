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
    if current_rs > 0 and last60_rs >= 0:
        strength_assessment = "strong"
        strength_interpretation = "시장보다 강했고 마감까지 상대강도가 유지됐습니다."
    elif current_rs > 0:
        strength_assessment = "mixed"
        strength_interpretation = "당일에는 시장보다 강했지만 마감 구간 상대강도 확인이 더 필요합니다."
    elif last60_rs >= 0:
        strength_assessment = "mixed"
        strength_interpretation = "당일에는 시장보다 약했지만 마감 구간에서 일부 회복했습니다."
    else:
        strength_assessment = "weak"
        strength_interpretation = "당일과 마감 구간 모두 시장 대비 강도가 부족했습니다."
    if close < vwap:
        execution_interpretation = "종가가 VWAP 아래에 있어 가격 구조를 추가로 확인해야 합니다."
    elif action in {"buy", "conditional_buy"}:
        execution_interpretation = "VWAP 위에서 마감해 눌림·돌파 가격을 고정할 수 있습니다."
    elif action == "not_suitable":
        execution_interpretation = "종가는 VWAP 위에서 마감했지만 현재 계좌 한도로는 진입 계획을 제공하지 않습니다."
    else:
        execution_interpretation = "종가는 VWAP 위에서 마감했지만 다른 직접 매수 조건이 충족되지 않았습니다."
    return [
        {
            "code": "market_strength",
            "label": "시장 대비 강도",
            "primaryValue": f"SPY 대비 {current_rs:+.2f}%p",
            "secondaryValue": f"마감 전 60분 {last60_rs:+.2f}%p",
            "assessment": strength_assessment,
            "interpretation": strength_interpretation,
        },
        {
            "code": "participation",
            "label": "거래 참여",
            "primaryValue": f"직전 정규장 동시간 대비 {volume:.2f}배",
            "secondaryValue": "확대 기준 1.00배",
            "assessment": "strong" if volume >= 1.0 else "weak",
            "interpretation": "가격 움직임에 거래량이 동반됐습니다." if volume >= 1.0 else "가격 움직임을 뒷받침하는 거래량이 부족합니다.",
        },
        {
            "code": "execution_structure",
            "label": "진입 구조",
            "primaryValue": f"종가 ${close:.2f} · VWAP ${vwap:.2f}",
            "secondaryValue": f"스프레드 {spread:.2f}bp · ATR ${atr:.2f}",
            "assessment": "strong" if close >= vwap else "mixed",
            "interpretation": execution_interpretation,
        },
    ]


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
        "volume_confirmation": "가격 움직임을 뒷받침하는 거래량이 충분하지 않았습니다.",
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
        headline = "시장 대비 강세가 마감까지 이어졌고 거래량도 가격 움직임을 뒷받침했습니다."
        body_sentences = interpretations[:3]
    elif action == "conditional_buy":
        reason = counter_sentence or "직접 매수 전에 추가 확인이 필요한 조건이 남아 있습니다."
        headline = f"시장 대비 강세와 거래 참여는 확인됐지만, {reason}"
        body_sentences = interpretations[:3]
    elif action == "not_suitable":
        headline = "시장 근거와 별개로 현재 계좌 한도에서는 신규 진입에 적합하지 않습니다."
        body_sentences = [*interpretations[:2], counter_sentence or "현재 계좌의 위험예산·현금·집중도 한도에서 한 주 이상 배정할 수 없습니다."]
    else:
        headline = counter_sentence or "직접 매수 기준을 충족하지 못해 관찰 대상으로 유지합니다."
        body_sentences = interpretations[:3]
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
            "promptVersion": "recommendation-decision-renderer.ko.v2",
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
