from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


CONTRACT_VERSION = "coach-report.v2"
SIMILARITY_WEIGHTS = {
    "directionScore": 0.16,
    "symbolIndustryScore": 0.14,
    "marketRegimeScore": 0.12,
    "trendMomentumScore": 0.16,
    "volumeScore": 0.10,
    "eventScore": 0.10,
    "portfolioStateScore": 0.10,
    "indicatorScore": 0.12,
}
ALERT_PROPOSAL_SOURCES = {"daily_trade", "entry_habit", "exit_habit", "portfolio_risk"}
EXIT_PRE_SALE_GIVEBACK_THRESHOLD_PERCENT = 3.0
EXIT_POST_SALE_MFE_THRESHOLD_PERCENT = 5.0
EXIT_HABIT_MIN_OCCURRENCES = 5
EXIT_PRE_SALE_MIN_BARS = 5
EXIT_POST_SALE_HORIZON = 20
EXPECTED_DECISION_CHECK_KEYS = frozenset({
    "chart.rsi",
    "chart.macd",
    "chart.volume",
    "news.company",
    "fundamentals.earnings",
    "market.context",
})
DECISION_CHECK_LABELS = {
    "chart.rsi": "RSI",
    "chart.macd": "MACD",
    "chart.volume": "거래량",
    "news.company": "기업 뉴스",
    "fundamentals.earnings": "실적 일정",
    "market.context": "시장 상황",
}


@dataclass(frozen=True)
class CoachInputSnapshot:
    request: dict[str, Any]
    user: dict[str, Any]
    fills: tuple[dict[str, Any], ...]
    positionsBefore: tuple[dict[str, Any], ...]
    positionsAfter: tuple[dict[str, Any], ...]
    portfolioBefore: dict[str, Any]
    portfolioAfter: dict[str, Any]
    marketContext: dict[str, Any]
    chartContext: dict[str, Any]
    indicatorContext: dict[str, Any]
    newsContext: dict[str, Any]
    fundamentalsContext: dict[str, Any]
    earningsContext: dict[str, Any]
    ontologyContext: dict[str, Any]
    sourceAsOf: dict[str, str | None]
    missingData: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CoachInputSnapshot":
        def obj(key: str) -> dict[str, Any]:
            value = raw.get(key)
            return dict(value) if isinstance(value, dict) else {}

        def rows(key: str) -> tuple[dict[str, Any], ...]:
            value = raw.get(key)
            return tuple(dict(item) for item in value if isinstance(item, dict)) if isinstance(value, list) else ()

        source = obj("sourceAsOf")
        return cls(
            request=obj("request"), user=obj("user"), fills=rows("fills"),
            positionsBefore=rows("positionsBefore"), positionsAfter=rows("positionsAfter"),
            portfolioBefore=obj("portfolioBefore"), portfolioAfter=obj("portfolioAfter"),
            marketContext=obj("marketContext"), chartContext=obj("chartContext"),
            indicatorContext=obj("indicatorContext"), newsContext=obj("newsContext"),
            fundamentalsContext=obj("fundamentalsContext"), earningsContext=obj("earningsContext"),
            ontologyContext=obj("ontologyContext"),
            sourceAsOf={str(key): str(value) if value is not None else None for key, value in source.items()},
            missingData=rows("missingData"),
        )


def build_coach_report(raw_snapshot: Any, analysis_id: str, *, generated_at: str | None = None) -> dict[str, Any] | None:
    if not isinstance(raw_snapshot, dict):
        return None
    snapshot = CoachInputSnapshot.from_dict(raw_snapshot)
    warnings: list[str] = []
    if not snapshot.fills:
        warnings.append("오늘 체결 데이터가 없습니다.")
    page = _build_page(snapshot, warnings) if snapshot.fills else None
    page2 = _build_habit_page(snapshot)
    page3 = _build_improvement_page(page2)
    page4 = _build_action_center(snapshot, page, page3)
    archive = raw_snapshot.get("_archive") if isinstance(raw_snapshot.get("_archive"), dict) else None
    return {
        "contractVersion": CONTRACT_VERSION,
        "analysisId": analysis_id,
        "generatedAt": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sourceAsOf": snapshot.sourceAsOf,
        "page1": page,
        "page2": page2,
        "page3": page3,
        "page4": page4,
        "snapshotRef": archive.get("key") if archive else None,
        "snapshotDigest": archive.get("sha256") if archive else None,
        "missingData": list(snapshot.missingData),
        "warnings": warnings,
    }


def _build_page(snapshot: CoachInputSnapshot, warnings: list[str]) -> dict[str, Any]:
    trades = [_trade_summary(fill, snapshot) for fill in snapshot.fills]
    selected_id = str(snapshot.request.get("selectedFillId") or trades[0]["fillId"])
    candidates = snapshot.chartContext.get("historicalCases")
    candidate_rows = [item for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else []
    reviews: dict[str, dict[str, Any]] = {}
    for fill in snapshot.fills:
        fill_id = str(fill.get("fillId") or "")
        current_cases = snapshot.chartContext.get("currentCasesByFillId")
        chart = current_cases.get(fill_id) if isinstance(current_cases, dict) else None
        if not isinstance(chart, dict) and fill_id == selected_id:
            chart = snapshot.chartContext.get("currentCase")
        fill_with_features = {**fill, **(chart if isinstance(chart, dict) else {})}
        current_case = _trade_case(fill_with_features, chart, current=True)
        current_case["missedChecks"] = _missed_markers(snapshot, fill_id)
        reviews[fill_id] = {
            "decisionAssessment": _decision_assessment(fill, snapshot),
            "currentCase": current_case,
            "similarCases": select_similar_cases(fill_with_features, candidate_rows),
            "checklist": _checklist(snapshot, fill_id),
            "portfolioImpact": _portfolio_impact(snapshot, fill),
            "watchConditions": _watch_conditions(current_case),
            "proposedAlerts": [],
            "confidence": _confidence(snapshot, fill_id),
        }
    if not any(review["similarCases"] for review in reviews.values()):
        warnings.append("유사 사례 부족")
    selected_id = selected_id if selected_id in reviews else next(iter(reviews))
    selected_review = reviews[selected_id]
    return {
        "selectedFillId": selected_id,
        "trades": trades,
        **selected_review,
        "reviewsByFillId": reviews,
    }


def select_similar_cases(current: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry_at = _timestamp(current.get("filledAt") or current.get("entryAt"))
    if entry_at is None:
        return []
    current_ids = {str(value) for value in (current.get("fillId"), current.get("caseId"), current.get("orderId")) if value}
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_ids = {str(value) for value in (candidate.get("fillId"), candidate.get("caseId"), candidate.get("orderId")) if value}
        if current_ids & candidate_ids:
            continue
        candidate_entry = _timestamp(candidate.get("entryAt") or candidate.get("tradeDate"))
        feature_as_of = _timestamp(candidate.get("featureAsOf"))
        # Similarity features must be an auditable point-in-time view. A case
        # without a feature timestamp cannot prove that post-entry data was not
        # used, so it is not eligible for deterministic ranking.
        if candidate_entry is None or candidate_entry > entry_at or feature_as_of is None or feature_as_of > candidate_entry:
            continue
        components = _similarity_components(current, candidate)
        available_weight = sum(SIMILARITY_WEIGHTS[key] for key in components)
        if available_weight <= 0:
            continue
        total = round(sum(components[key] * SIMILARITY_WEIGHTS[key] for key in components) / available_weight, 4)
        case = _trade_case(candidate, candidate, current=False)
        case["similarityComponents"] = components
        case["similarityScore"] = round(total * 100, 2)
        if not case.get("mistakeSummary"):
            missed_labels = [
                str(item.get("label") or item.get("checkKey") or "확인 항목")
                for item in candidate.get("decisionChecks", [])
                if isinstance(item, dict) and item.get("status") == "unchecked"
            ]
            case["mistakeSummary"] = (
                f"확인 누락: {' · '.join(missed_labels[:3])}"
                if missed_labels else "확인 기록 없음"
            )
        case["sameAsToday"] = case.get("sameAsToday") or _comparison_summary(current, candidate, same=True)
        case["differentFromToday"] = case.get("differentFromToday") or _comparison_summary(current, candidate, same=False)
        ranked.append(case)
    ranked.sort(key=lambda item: (-float(item["similarityScore"]), str(item["caseId"])))
    return ranked[:6]


def _comparison_summary(current: dict[str, Any], candidate: dict[str, Any], *, same: bool) -> str:
    labels = {
        "side": "매수·매도 방향",
        "sector": "섹터",
        "marketRegime": "시장 국면",
        "trend": "추세",
        "momentum": "모멘텀",
        "volumeState": "거래량 상태",
        "rsiBand": "RSI 구간",
        "macdState": "MACD 상태",
        "eventState": "이벤트 상태",
    }
    matches: list[str] = []
    for key, label in labels.items():
        left = current.get(key)
        right = candidate.get(key)
        if left is None or right is None:
            continue
        if (left == right) is same:
            matches.append(label)
    prefix = "같은 조건" if same else "다른 조건"
    return f"{prefix}: {' · '.join(matches[:4])}" if matches else f"{prefix}을 계산할 데이터가 부족합니다."


def calculate_outcomes(entry_price: Any, exit_price: Any, series: list[dict[str, Any]], side: str) -> dict[str, float | None]:
    entry = _number(entry_price)
    if not entry or entry <= 0:
        return {"returnPercent": None, "mfePercent": None, "maePercent": None}
    direction = -1.0 if str(side).lower() == "sell" else 1.0
    highs = [_number(item.get("high")) for item in series]
    lows = [_number(item.get("low")) for item in series]
    valid_highs = [value for value in highs if value is not None]
    valid_lows = [value for value in lows if value is not None]
    favorable = ((max(valid_highs) - entry) / entry * 100) if direction > 0 and valid_highs else ((entry - min(valid_lows)) / entry * 100 if valid_lows else None)
    adverse = ((min(valid_lows) - entry) / entry * 100) if direction > 0 and valid_lows else ((entry - max(valid_highs)) / entry * 100 if valid_highs else None)
    exit_value = _number(exit_price)
    result = direction * ((exit_value - entry) / entry * 100) if exit_value is not None else None
    return {
        "returnPercent": round(result, 4) if result is not None else None,
        "mfePercent": round(max(0.0, favorable), 4) if favorable is not None else None,
        "maePercent": round(min(0.0, adverse), 4) if adverse is not None else None,
    }


def _similarity_components(left: dict[str, Any], right: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    def available(key: str) -> bool: return left.get(key) is not None and right.get(key) is not None
    def same(key: str) -> float: return 1.0 if left.get(key) == right.get(key) else 0.0
    if available("side"): result["directionScore"] = same("side")
    if available("symbol"): result["symbolIndustryScore"] = 1.0 if same("symbol") else (0.7 if available("sector") and same("sector") else 0.0)
    elif available("sector"): result["symbolIndustryScore"] = 0.7 * same("sector")
    if available("marketRegime"): result["marketRegimeScore"] = same("marketRegime")
    trend_parts = [same(key) for key in ("trend", "momentum") if available(key)]
    if trend_parts: result["trendMomentumScore"] = sum(trend_parts) / len(trend_parts)
    if available("volumeState"): result["volumeScore"] = same("volumeState")
    if available("eventState"): result["eventScore"] = same("eventState")
    portfolio_parts = [same(key) for key in ("concentrationBand", "cashBand") if available(key)]
    if portfolio_parts: result["portfolioStateScore"] = sum(portfolio_parts) / len(portfolio_parts)
    indicator_parts = [same(key) for key in ("rsiBand", "macdState") if available(key)]
    if indicator_parts: result["indicatorScore"] = sum(indicator_parts) / len(indicator_parts)
    return result


def _trade_case(source: dict[str, Any], chart: Any, *, current: bool) -> dict[str, Any]:
    chart_data = dict(chart) if isinstance(chart, dict) else source
    series = [dict(item) for item in chart_data.get("series", []) if isinstance(item, dict)]
    entry = source.get("entryPrice") or source.get("averageFillPrice")
    outcome_series = _outcome_series(series, source)
    outcomes = calculate_outcomes(entry, source.get("exitPrice") or source.get("evaluationPrice") or (outcome_series[-1].get("close") if outcome_series else None), outcome_series, str(source.get("side") or "buy"))
    holding_duration = source.get("holdingDuration")
    evaluation_horizon = max((int(item.get("relativeDay", 0)) for item in outcome_series), default=None)
    missed_checks = [dict(item) for item in source.get("missedChecks", []) if isinstance(item, dict)]
    if not missed_checks:
        for check in source.get("decisionChecks", []) if isinstance(source.get("decisionChecks"), list) else []:
            marker = check.get("marker") if isinstance(check, dict) else None
            if check.get("status") == "unchecked" and isinstance(marker, dict):
                missed_checks.append(dict(marker))
    return {
        "caseId": str(source.get("caseId") or source.get("fillId") or ("current" if current else "unknown")),
        "tradeDate": source.get("tradeDate") or source.get("filledAt"), "symbol": source.get("symbol"),
        "side": source.get("side"), "entryPrice": entry, "exitPrice": source.get("exitPrice"),
        "currentPrice": source.get("currentPrice"),
        **outcomes, "holdingDuration": holding_duration, "evaluationHorizonDays": evaluation_horizon, "series": series,
        "missedChecks": missed_checks,
        "mistakeSummary": source.get("mistakeSummary"), "sameAsToday": source.get("sameAsToday"),
        "differentFromToday": source.get("differentFromToday"),
    }


def _trade_summary(fill: dict[str, Any], snapshot: CoachInputSnapshot) -> dict[str, Any]:
    entry = _number(fill.get("averageFillPrice")); current = _number(fill.get("currentPrice"))
    direction = -1 if str(fill.get("side")).lower() == "sell" else 1
    result = direction * (current - entry) / entry * 100 if entry and current is not None else None
    earnings = snapshot.earningsContext.get(str(fill.get("symbol"))) if isinstance(snapshot.earningsContext.get(str(fill.get("symbol"))), dict) else {}
    impact = _portfolio_impact(snapshot, fill)
    return {**{key: fill.get(key) for key in ("fillId", "symbol", "companyName", "side", "filledAt", "averageFillPrice", "quantity", "currentPrice")},
            "weightBefore": impact.get("symbolWeightBefore"), "weightAfter": impact.get("symbolWeightAfter"),
            "currentReturnPercent": round(result, 4) if result is not None else None,
            "earningsAt": earnings.get("earningsAt"), "earningsDaysRemaining": earnings.get("earningsDaysRemaining")}


def _decision_assessment(fill: dict[str, Any], snapshot: CoachInputSnapshot) -> dict[str, Any]:
    fill_id = str(fill.get("fillId") or "")
    by_fill = snapshot.request.get("decisionChecksByFillId")
    rows = by_fill.get(fill_id) if isinstance(by_fill, dict) else None
    checks = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    missed = [item for item in checks if item.get("status") == "unchecked"]
    recorded_keys = {str(item.get("checkKey") or "") for item in checks} & EXPECTED_DECISION_CHECK_KEYS
    if not checks:
        grade = "insufficient_data"; process = "확인 기록 없음"; summary = "손익과 별개로 판단 절차를 평가할 확인 기록이 없습니다."
    elif len(missed) >= 2:
        grade = "risk"; process = f"{len(missed)}개 핵심 조건 미확인"; summary = "여러 핵심 확인 절차를 건너뛴 판단이었습니다."
    elif missed:
        grade = "attention"; process = f"{missed[0].get('label') or '핵심 조건'} 미확인"; summary = "결과와 무관하게 보완해야 할 확인 절차가 있습니다."
    elif recorded_keys != EXPECTED_DECISION_CHECK_KEYS:
        grade = "insufficient_data"; process = f"확인 기록 불완전 ({len(recorded_keys)}/{len(EXPECTED_DECISION_CHECK_KEYS)})"; summary = "기록된 항목만으로 전체 판단 절차가 적절했다고 평가할 수 없습니다."
    else:
        grade = "good"; process = "기록된 확인 절차 충족"; summary = "기록된 범위에서는 필요한 판단 절차를 확인했습니다."
    return {
        "grade": grade, "summary": summary, "processAssessment": process,
        "outcomeAssessment": _trade_summary(fill, snapshot)["currentReturnPercent"],
        "evidence": [str(item.get("evidence") or item.get("label") or "") for item in checks if item.get("evidence") or item.get("label")],
        "sourceAsOf": snapshot.sourceAsOf,
    }


def _checklist(snapshot: CoachInputSnapshot, fill_id: str) -> dict[str, list[dict[str, Any]]]:
    by_fill = snapshot.request.get("decisionChecksByFillId")
    rows = by_fill.get(fill_id) if isinstance(by_fill, dict) else None
    checks = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    result: dict[str, list[dict[str, Any]]] = {key: [] for key in ("chart", "news", "fundamentals", "market")}
    for item in checks:
        category = str(item.get("category") or "")
        if category in result:
            result[category].append({key: item.get(key) for key in ("status", "label", "checkedAt", "evidence", "source", "sourceAsOf")})
    defaults = {"chart": "차트 확인 기록", "news": "뉴스 확인 기록", "fundamentals": "재무 확인 기록", "market": "시장 확인 기록"}
    for category, label in defaults.items():
        if not result[category]:
            result[category].append({"status": "insufficient_data", "label": label, "checkedAt": None, "evidence": "확인 기록 없음", "source": None, "sourceAsOf": None})
    return result


def _portfolio_impact(snapshot: CoachInputSnapshot, fill: dict[str, Any]) -> dict[str, Any]:
    fill_id = str(fill.get("fillId") or "")
    before_by_fill = snapshot.portfolioBefore.get("byFillId")
    after_by_fill = snapshot.portfolioAfter.get("byFillId")
    before = before_by_fill.get(fill_id) if isinstance(before_by_fill, dict) else snapshot.portfolioBefore.get("selected")
    after = after_by_fill.get(fill_id) if isinstance(after_by_fill, dict) else snapshot.portfolioAfter.get("selected")
    before_metrics = _portfolio_metrics(before if isinstance(before, dict) else {}, str(fill.get("symbol") or ""), str(fill.get("sector") or ""))
    after_metrics = _portfolio_metrics(after if isinstance(after, dict) else {}, str(fill.get("symbol") or ""), str(fill.get("sector") or ""))
    result = {f"{key}Before": before_metrics.get(key) for key in ("symbolWeight", "sectorWeight", "cashWeight", "topHoldingsConcentration")}
    result.update({f"{key}After": after_metrics.get(key) for key in ("symbolWeight", "sectorWeight", "cashWeight", "topHoldingsConcentration")})
    result["valuationBasisBefore"] = before.get("valuationBasis") if isinstance(before, dict) else None
    result["valuationBasisAfter"] = after.get("valuationBasis") if isinstance(after, dict) else None
    flags: list[str] = []
    if _increased(before_metrics, after_metrics, "symbolWeight"): flags.append("단일 종목 위험 증가")
    if _increased(before_metrics, after_metrics, "sectorWeight"): flags.append("섹터 집중도 상승")
    if _decreased(before_metrics, after_metrics, "cashWeight"): flags.append("현금 완충력 감소")
    if _increased(before_metrics, after_metrics, "topHoldingsConcentration"): flags.append("상위 종목 집중도 상승")
    result["riskFlags"] = flags
    return result


def _watch_conditions(case: dict[str, Any]) -> list[dict[str, Any]]:
    series = [item for item in case.get("series", []) if isinstance(item, dict)]
    observed = [item for item in series if int(item.get("relativeDay", -999)) <= 0]
    if not observed:
        return []
    latest = observed[-1]; symbol = str(case.get("symbol") or "")
    current_price = _number(case.get("currentPrice"))
    observed_price = current_price if current_price is not None else latest.get("close")
    prior = observed[-20:]
    lows = [_number(item.get("low")) for item in prior]; highs = [_number(item.get("high")) for item in prior]
    support = min((value for value in lows if value is not None), default=None)
    resistance = max((value for value in highs if value is not None), default=None)
    conditions: list[dict[str, Any]] = []
    for suffix, label, threshold, operator, action in (
        ("support", f"{support:.2f} 아래 일봉 마감" if support is not None else "지지선 이탈", support, "<", "비중 축소 검토"),
        ("resistance", f"다음 저항 {resistance:.2f} 접근" if resistance is not None else "저항선 접근", resistance, ">=", "분할 청산 검토"),
    ):
        if threshold is not None:
            conditions.append({"id": f"{case.get('caseId')}-{suffix}", "type": "price", "label": label, "currentValue": observed_price, "threshold": round(threshold, 4), "operator": operator, "reason": "최근 20개 완료 일봉의 가격 수준과 체결 이후 현재가 비교", "recommendedAction": action, "alertSupported": True, "alertRequest": {"symbol": symbol, "type": "price_cross", "targetPrice": str(round(threshold, 4)), "repeatLimit": 1}})
    if latest.get("relativeVolume") is not None:
        conditions.append({"id": f"{case.get('caseId')}-volume", "type": "volume", "label": "상대 거래량 1.2 회복", "currentValue": latest.get("relativeVolume"), "threshold": 1.2, "operator": ">=", "reason": "참여 강도 회복 확인", "recommendedAction": "재관찰", "alertSupported": False})
    if latest.get("rsi") is not None:
        conditions.append({"id": f"{case.get('caseId')}-rsi", "type": "rsi", "label": "RSI 과열 구간 이탈", "currentValue": latest.get("rsi"), "threshold": 70, "operator": "<", "reason": "과열 완화 여부 확인", "recommendedAction": "추세 재평가", "alertSupported": False})
    return conditions


def _confidence(snapshot: CoachInputSnapshot, fill_id: str) -> dict[str, Any]:
    by_fill = snapshot.request.get("decisionChecksByFillId")
    checks = by_fill.get(fill_id) if isinstance(by_fill, dict) else None
    keys = {
        str(item.get("checkKey") or "")
        for item in checks if isinstance(item, dict)
    } & EXPECTED_DECISION_CHECK_KEYS if isinstance(checks, list) else set()
    available = len(keys)
    return {
        "level": "medium" if keys == EXPECTED_DECISION_CHECK_KEYS else "low",
        "reason": f"필수 확인 기록 {available}/{len(EXPECTED_DECISION_CHECK_KEYS)}건" if available else "확인 기록 없음",
    }


def _build_habit_page(snapshot: CoachInputSnapshot) -> dict[str, Any] | None:
    rows = snapshot.chartContext.get("historicalCases")
    cases = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    requested_at = datetime.fromtimestamp(_timestamp(snapshot.request.get("requestedAt")) or datetime.now(timezone.utc).timestamp(), tz=timezone.utc)
    requested_timestamp = requested_at.timestamp()
    reports_by_period: dict[str, Any] = {}
    for key, days, label in (("6m", 183, "최근 6개월"),):
        cutoff = requested_at.timestamp() - days * 86400
        period_cases = [
            item
            for item in cases
            if (case_timestamp := _timestamp(item.get("tradeDate") or item.get("filledAt"))) is not None
            and cutoff <= case_timestamp <= requested_timestamp
        ]
        reports_by_period[key] = {
            "entry": _habit_report("entry", [item for item in period_cases if str(item.get("side")).lower() == "buy"], label, requested_timestamp),
            "exit": _habit_report("exit", [item for item in period_cases if str(item.get("side")).lower() == "sell"], label, requested_timestamp),
            "portfolio": _portfolio_habit_report(snapshot, period_cases, label, cutoff, requested_timestamp),
        }
    has_samples = any(report.get("sampleSize", 0) for reports in reports_by_period.values() for report in reports.values())
    return {"availability": "ready" if has_samples else "insufficient_sample", "defaultPeriod": "6m", "reportsByPeriod": reports_by_period}


def _habit_report(stage: str, cases: list[dict[str, Any]], period_label: str, requested_timestamp: float) -> dict[str, Any]:
    total_trade_count = len(cases)
    analyzable_cases = [
        item for item in cases
        if _timestamp(item.get("tradeDate") or item.get("filledAt")) is not None
    ]
    outcomes = [_trade_case(item, item, current=False) for item in analyzable_cases]
    returns = [float(item["returnPercent"]) for item in outcomes if _number(item.get("returnPercent")) is not None]
    mfes = [float(item["mfePercent"]) for item in outcomes if _number(item.get("mfePercent")) is not None]
    maes = [float(item["maePercent"]) for item in outcomes if _number(item.get("maePercent")) is not None]
    cases_with_checks = [
        item for item in analyzable_cases
        if isinstance(item.get("decisionChecks"), list) and any(isinstance(check, dict) for check in item["decisionChecks"])
    ]
    excluded_trade_count = max(0, total_trade_count - len(analyzable_cases))
    sample = len(analyzable_cases)
    confidence = _habit_evidence_quality(sample, len(cases_with_checks))
    decision_record_rate = round(len(cases_with_checks) / sample * 100, 4) if sample else None
    insights: list[dict[str, Any]] = []
    overbought = [item for item in analyzable_cases if item.get("rsiBand") == "overbought"]
    if stage == "entry" and len(overbought) >= 5:
        over_outcomes = [_trade_case(item, item, current=False) for item in overbought]
        avg_return = _average([item.get("returnPercent") for item in over_outcomes])
        insights.append({"id": "entry-overbought", "stage": "entry", "kind": "improvement_candidate" if (avg_return or 0) < 0 else "observation", "title": "과열 진입 전 지표 재확인", "condition": "진입 시 RSI 70 이상", "observedBehavior": "과거 과열 진입에서는 RSI뿐 아니라 거래량, MACD, 지지·저항, 실적 일정을 함께 확인해야 판단 품질을 비교할 수 있었습니다.", "sampleSize": len(overbought), "confidence": "low" if len(overbought) < 15 else "high", "metrics": {"avgReturn": avg_return}, "recurrenceScore": min(1.0, len(overbought) / max(1, sample)), "impactScore": min(1.0, abs(avg_return or 0) / 10), "controllabilityScore": 0.9, "nextAction": "진입 전 거래량·RSI·MACD·저항선·실적 일정을 한 번에 확인"})
    if stage == "exit":
        insights.extend(_exit_habit_insights(analyzable_cases, requested_timestamp))
    availability = "ready" if sample >= 5 else "insufficient_sample"
    if stage == "exit":
        summary = (
            f"{sample}건의 매도 체결 후 가격 경로를 매도 체결가 기준으로 집계했습니다. "
            "이 값은 실현 손익이나 당시 판단 품질을 뜻하지 않습니다."
            if sample
            else "분석할 매도 후 가격 경로 표본이 없습니다."
        )
        behavior = [
            {"label": "매도 후 최종 가격 경로(매도 관점)", "value": _coach_value(_average(returns), "%")},
            {"label": "매도 후 최대 유리 경로(매도 관점)", "value": _coach_value(_average(mfes), "%")},
            {"label": "매도 후 최대 불리 경로(매도 관점)", "value": _coach_value(_average(maes), "%")},
            {"label": "판단 확인 기록 보유 거래", "value": _coach_value(decision_record_rate, "%")},
        ]
    else:
        summary = (
            f"{sample}건의 진입 결과와 판단 확인 기록 보유 여부를 분리해 집계했습니다."
            if sample else "분석할 거래 표본이 없습니다."
        )
        behavior = [
            {"label": "평균 수익률", "value": _coach_value(_average(returns), "%")},
            {"label": "평균 MFE", "value": _coach_value(_average(mfes), "%")},
            {"label": "평균 MAE", "value": _coach_value(_average(maes), "%")},
            {"label": "판단 확인 기록 보유 거래", "value": _coach_value(decision_record_rate, "%")},
        ]
    excluded_reasons = ["체결 시각 확인 불가"] if excluded_trade_count else []
    return {
        "stage": stage, "availability": availability, "periodLabel": period_label,
        "sampleSize": sample, "totalTradeCount": total_trade_count, "analyzedTradeCount": sample,
        "excludedTradeCount": excluded_trade_count, "excludedReasons": excluded_reasons,
        "confidence": confidence, "evidenceQuality": confidence,
        "missingData": [] if sample else ["표본 부족"], "summary": summary, "behavior": behavior,
        "planConsistency": {"availability": "no_confirmation_record"},
        "longTermProfile": _long_term_profile(analyzable_cases, outcomes, total_trade_count), "insights": insights,
    }


def _habit_evidence_quality(analyzed_count: int, recorded_count: int) -> str:
    if analyzed_count < 5:
        return "insufficient"
    recorded_rate = recorded_count / analyzed_count if analyzed_count else 0.0
    if analyzed_count >= 50 and recorded_rate >= 0.7:
        return "high"
    if analyzed_count >= 20 and recorded_rate >= 0.4:
        return "medium"
    return "low"


def _long_term_profile(cases: list[dict[str, Any]], outcomes: list[dict[str, Any]], total_trade_count: int | None = None) -> dict[str, Any]:
    """Summarize durable decision habits without treating profit as process quality.

    A confirmed process requires every allowlisted decision check to be present and
    checked.  All other cases stay explicitly unconfirmed; this avoids inventing a
    trading plan from a profitable or unprofitable outcome.
    """
    rows: list[dict[str, Any]] = []
    missed_by_key: dict[str, list[dict[str, Any]]] = {}
    symbol_rows: dict[str, list[dict[str, Any]]] = {}
    for index, source in enumerate(cases):
        outcome = outcomes[index] if index < len(outcomes) else {}
        checks = [item for item in source.get("decisionChecks", []) if isinstance(item, dict)] if isinstance(source.get("decisionChecks"), list) else []
        recorded_keys = {str(item.get("checkKey") or "") for item in checks} & EXPECTED_DECISION_CHECK_KEYS
        missed = [item for item in checks if item.get("status") == "unchecked"]
        process = "confirmed" if recorded_keys == EXPECTED_DECISION_CHECK_KEYS and not missed else "unconfirmed"
        missed_labels: list[str] = []
        for item in missed:
            key = str(item.get("checkKey") or "")
            label = str(item.get("label") or DECISION_CHECK_LABELS.get(key) or key or "확인 항목")
            missed_labels.append(label)
            missed_by_key.setdefault(key or label, []).append({"label": label, "returnPercent": outcome.get("returnPercent"), "maePercent": outcome.get("maePercent")})
        row = {
            "caseId": outcome.get("caseId") or source.get("caseId") or source.get("fillId"),
            "symbol": source.get("symbol"), "side": source.get("side"),
            "tradeDate": source.get("tradeDate") or source.get("filledAt"),
            "process": process, "recorded": bool(checks), "missed": bool(missed),
            "missedLabels": missed_labels, "returnPercent": _number(outcome.get("returnPercent")),
            "maePercent": _number(outcome.get("maePercent")),
        }
        rows.append(row)
        symbol = str(source.get("symbol") or "").upper()
        if symbol:
            symbol_rows.setdefault(symbol, []).append(row)

    sample = len(rows)
    total = total_trade_count if total_trade_count is not None else sample
    basis = f"최근 6개월 전체 {total}건 중 분석 가능한 {sample}건 기준으로" if total != sample else f"최근 6개월 {sample}건 기준으로"
    recorded_count = sum(1 for row in rows if row["recorded"])
    confirmed_count = sum(1 for row in rows if row["process"] == "confirmed")
    missed_count = sum(1 for row in rows if row["missed"])
    unconfirmed_count = sample - confirmed_count
    process_outcome: list[dict[str, Any]] = []
    for process in ("confirmed", "unconfirmed"):
        for outcome in ("positive", "negative"):
            group = [row for row in rows if row["process"] == process and row["returnPercent"] is not None and ((row["returnPercent"] >= 0) == (outcome == "positive"))]
            process_outcome.append({
                "process": process, "outcome": outcome, "count": len(group),
                "averageReturnPercent": _average([row["returnPercent"] for row in group]),
                "averageMaePercent": _average([row["maePercent"] for row in group]),
            })

    patterns: list[dict[str, Any]] = []
    if sample and recorded_count < sample:
        count = sample - recorded_count
        patterns.append(_habit_pattern(
            "decision-record-gap", "판단 확인 기록 공백", count, sample,
            f"{sample}건 중 {count}건은 주문 전 확인 기록이 없어 판단 과정의 적절성을 검증할 수 없습니다.",
            [row for row in rows if not row["recorded"]],
        ))
    for key, values in sorted(missed_by_key.items(), key=lambda item: (-len(item[1]), item[0]))[:2]:
        label = str(values[0].get("label") or DECISION_CHECK_LABELS.get(key) or key)
        patterns.append(_habit_pattern(
            f"missed-{key}", f"{label} 확인 누락", len(values), sample,
            f"{sample}건 중 {len(values)}건에서 {label} 확인이 미완료로 기록됐습니다.", values,
        ))
    if sample:
        top_symbol, top_symbol_rows = max(symbol_rows.items(), key=lambda item: (len(item[1]), item[0]), default=("", []))
        if top_symbol and len(top_symbol_rows) >= 2:
            patterns.append(_habit_pattern(
                f"symbol-{top_symbol}", f"{top_symbol} 거래 편중", len(top_symbol_rows), sample,
                f"선택 기간 거래의 {len(top_symbol_rows)}/{sample}건이 {top_symbol}에 집중됐습니다.", top_symbol_rows,
            ))
    patterns = patterns[:3]
    representatives = _representative_habit_trades(rows)
    if sample < 5:
        headline = f"{basis}, 장기 투자 성향을 단정하기에는 표본이 부족합니다."
    elif patterns:
        lead = patterns[0]
        headline = f"{basis}, {lead['occurrenceCount']}건에서 {lead['title']} 패턴이 반복됐습니다. 수익 여부보다 어떤 조건을 확인했는지 먼저 봐야 합니다."
    elif unconfirmed_count:
        headline = f"{basis}, {unconfirmed_count}건은 필수 확인 기록이 완전하지 않아 판단 과정의 일관성을 검증할 수 없습니다."
    else:
        headline = f"{basis}, 모든 분석 대상에 필수 확인 기록이 남아 있어 결과 손익과 판단 과정을 분리해 장기 습관을 비교할 수 있습니다."
    return {
        "headline": headline,
        "decisionRecords": {
            "recordedTradeCount": recorded_count,
            "confirmedTradeCount": confirmed_count,
            "unconfirmedTradeCount": unconfirmed_count,
            "missedCheckTradeCount": missed_count,
        },
        "processOutcome": process_outcome,
        "patterns": patterns,
        "representativeTrades": representatives,
    }


def _habit_pattern(identifier: str, title: str, count: int, sample: int, description: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": identifier, "title": title, "occurrenceCount": count,
        "occurrenceRatePercent": round(count / sample * 100, 4) if sample else None,
        "description": description,
        "averageReturnPercent": _average([row.get("returnPercent") for row in rows]),
        "averageMaePercent": _average([row.get("maePercent") for row in rows]),
        "confidence": "high" if count >= 15 else "medium" if count >= 8 else "low",
    }


def _representative_habit_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for process, outcome in (("unconfirmed", "negative"), ("confirmed", "negative"), ("unconfirmed", "positive")):
        candidates = [
            row for row in rows
            if row["process"] == process and row["returnPercent"] is not None and ((row["returnPercent"] >= 0) == (outcome == "positive"))
        ]
        if candidates:
            selected.append(max(candidates, key=lambda row: abs(float(row["returnPercent"]))))
    for row in sorted((row for row in rows if row["returnPercent"] is not None), key=lambda item: -abs(float(item["returnPercent"]))):
        if row not in selected:
            selected.append(row)
        if len(selected) >= 3:
            break
    result: list[dict[str, Any]] = []
    for row in selected[:3]:
        missed = " · ".join(row["missedLabels"][:2]) if row["missedLabels"] else "필수 확인 기록 충족" if row["process"] == "confirmed" else "확인 기록이 완전하지 않음"
        result.append({
            "caseId": str(row.get("caseId") or "unknown"), "symbol": row.get("symbol"), "side": row.get("side"),
            "tradeDate": row.get("tradeDate"), "process": row["process"],
            "outcome": "positive" if float(row["returnPercent"] or 0) >= 0 else "negative",
            "returnPercent": row.get("returnPercent"), "maePercent": row.get("maePercent"), "reason": missed,
        })
    return result


def _exit_habit_insights(cases: list[dict[str, Any]], requested_timestamp: float) -> list[dict[str, Any]]:
    """Build conservative sell observations from auditable execution/candle data.

    Pre-sale giveback uses only completed T-60..T-1 candles. Post-sale MFE is
    explicitly hindsight evaluation, requires a complete T+20 horizon, and
    never treats the result as proof that the original exit plan was wrong.
    """
    pre_sale_observations: list[float] = []
    post_sale_observations: list[float] = []
    for case in cases:
        sold_at = _timestamp(case.get("filledAt") or case.get("tradeDate"))
        sold_price = _number(case.get("averageFillPrice"))
        if sold_at is None or sold_price is None or sold_price <= 0:
            continue

        points: list[tuple[int, float, float]] = []
        for item in case.get("series", []):
            if not isinstance(item, dict):
                continue
            try:
                relative_day = int(item.get("relativeDay"))
            except (TypeError, ValueError):
                continue
            point_at = _timestamp(item.get("time"))
            high = _number(item.get("high"))
            if point_at is None or high is None or high <= 0 or point_at > requested_timestamp:
                continue
            points.append((relative_day, point_at, high))

        pre_sale_by_day: dict[int, float] = {}
        for relative_day, point_at, high in points:
            if -60 <= relative_day <= -1 and point_at < sold_at:
                pre_sale_by_day[relative_day] = max(high, pre_sale_by_day.get(relative_day, high))
        if len(pre_sale_by_day) >= EXIT_PRE_SALE_MIN_BARS:
            peak = max(pre_sale_by_day.values())
            giveback = max(0.0, (peak - sold_price) / peak * 100)
            pre_sale_observations.append(round(giveback, 4))

        post_sale_by_day: dict[int, float] = {}
        for relative_day, point_at, high in points:
            if 1 <= relative_day <= EXIT_POST_SALE_HORIZON and point_at > sold_at:
                post_sale_by_day[relative_day] = max(high, post_sale_by_day.get(relative_day, high))
        # A shorter observed path is not comparable with a full T+20 path.
        # Wait until T+20 exists as of the immutable analysis cutoff.
        if EXIT_POST_SALE_HORIZON in post_sale_by_day:
            post_peak = max(post_sale_by_day.values())
            post_mfe = max(0.0, (post_peak - sold_price) / sold_price * 100)
            post_sale_observations.append(round(post_mfe, 4))

    insights: list[dict[str, Any]] = []
    givebacks = [value for value in pre_sale_observations if value >= EXIT_PRE_SALE_GIVEBACK_THRESHOLD_PERCENT]
    if len(givebacks) >= EXIT_HABIT_MIN_OCCURRENCES:
        average_giveback = _average(givebacks) or 0.0
        insights.append({
            "id": "exit-pre-sale-peak-giveback",
            "stage": "exit",
            "kind": "improvement_candidate",
            "title": "청산 전 고점 대비 반납 점검",
            "condition": f"실제 매도 체결가가 청산 전 최대 60개 완료 일봉 고점 대비 {EXIT_PRE_SALE_GIVEBACK_THRESHOLD_PERCENT:g}% 이상 낮음",
            "observedBehavior": f"{len(givebacks)}건에서 실제 매도 체결가가 청산 전 고점보다 평균 {average_giveback:.2f}% 낮았습니다. 사전 계획 기록이 없으므로 당시 판단 과정은 별도로 보아야 합니다.",
            "sampleSize": len(givebacks),
            "confidence": "high" if len(givebacks) >= 15 else "low",
            "metrics": {},
            "recurrenceScore": min(1.0, len(givebacks) / max(1, len(pre_sale_observations))),
            "impactScore": min(1.0, average_giveback / 10),
            "controllabilityScore": 0.75,
            "nextAction": "청산 전 고점 대비 반납률과 사전 청산 근거를 함께 기록",
        })

    post_sale_mfes = [value for value in post_sale_observations if value >= EXIT_POST_SALE_MFE_THRESHOLD_PERCENT]
    if len(post_sale_mfes) >= EXIT_HABIT_MIN_OCCURRENCES:
        average_post_mfe = _average(post_sale_mfes) or 0.0
        insights.append({
            "id": "exit-post-sale-mfe",
            "stage": "exit",
            "kind": "observation",
            "title": "청산 전 목표가·수익반납 확인",
            "condition": f"매도 후 T+1..T+{EXIT_POST_SALE_HORIZON} 완료 일봉의 MFE가 체결가 대비 {EXIT_POST_SALE_MFE_THRESHOLD_PERCENT:g}% 이상",
            "observedBehavior": f"사후 {EXIT_POST_SALE_HORIZON}개 일봉이 모두 확정된 {len(post_sale_mfes)}건에서 청산 후 추가 상승 여지가 확인됐습니다. 청산 전에는 목표가, 손절 기준, MFE 반납, 거래량 약화, MACD 전환을 함께 봐야 합니다.",
            "sampleSize": len(post_sale_mfes),
            "confidence": "high" if len(post_sale_mfes) >= 15 else "low",
            "metrics": {"avgMfe": average_post_mfe},
            "recurrenceScore": min(1.0, len(post_sale_mfes) / max(1, len(post_sale_observations))),
            "impactScore": min(1.0, average_post_mfe / 10),
            "controllabilityScore": 0.6,
            "nextAction": "다음 청산 계획에서 전량 청산과 분할 청산 기준을 먼저 비교",
        })
    return insights


def _portfolio_habit_report(
    snapshot: CoachInputSnapshot,
    cases: list[dict[str, Any]],
    period_label: str,
    cutoff: float,
    requested_timestamp: float,
) -> dict[str, Any]:
    history = snapshot.portfolioBefore.get("history")
    rows = _portfolio_habit_rows(history, cutoff, requested_timestamp)
    sample = len(rows)
    concentrations = [_portfolio_metrics(row, "", "").get("topHoldingsConcentration") for row in rows]
    valid = [float(value) for value in concentrations if _number(value) is not None]
    insights: list[dict[str, Any]] = []
    high_count = len([value for value in valid if value >= 60])
    if high_count >= 5:
        insights.append({"id": "portfolio-concentration", "stage": "portfolio", "kind": "improvement_candidate", "title": "상위 종목 집중", "condition": "상위 3개 종목 합산 비중 60% 이상", "observedBehavior": "높은 집중 상태가 반복되었습니다.", "sampleSize": high_count, "confidence": "low" if high_count < 15 else "high", "metrics": {"riskContribution": _average(valid)}, "recurrenceScore": high_count / max(1, sample), "impactScore": min(1.0, (_average(valid) or 0) / 100), "controllabilityScore": 0.8})
    diversification = _portfolio_market_diversification(snapshot, cutoff, requested_timestamp)
    confidence = "high" if sample >= 50 else "medium" if sample >= 20 else "low" if sample >= 5 else "insufficient"
    return {"stage": "portfolio", "availability": "ready" if sample >= 5 else "insufficient_sample", "periodLabel": period_label, "sampleSize": sample, "totalTradeCount": sample, "analyzedTradeCount": sample, "excludedTradeCount": 0, "excludedReasons": [], "confidence": confidence, "evidenceQuality": confidence, "missingData": [] if sample else ["포트폴리오 이력 부족"], "summary": f"{sample}개 포트폴리오 snapshot의 집중도를 집계했습니다." if sample else "포트폴리오 이력이 부족합니다.", "behavior": [{"label": "평균 상위 종목 집중도", "value": _coach_value(_average(valid), "%")}], "planConsistency": {"availability": "no_confirmation_record"}, "longTermProfile": _portfolio_long_term_profile(valid, sample, diversification), "insights": insights}


def _portfolio_long_term_profile(concentrations: list[float], sample: int, diversification: dict[str, Any]) -> dict[str, Any]:
    """Describe repeated concentration risk from immutable post-fill snapshots.

    This profile intentionally has no process/outcome cohorts or representative
    trades: portfolio snapshots prove exposure, not a user's pre-trade checklist.
    """
    high = [value for value in concentrations if value >= 60]
    patterns: list[dict[str, Any]] = []
    if high:
        patterns.append({
            "id": "top-holdings-concentration",
            "title": "상위 종목 집중 구간 반복",
            "occurrenceCount": len(high),
            "occurrenceRatePercent": round(len(high) / sample * 100, 4) if sample else None,
            "description": f"확정된 거래 후 snapshot {sample}건 중 {len(high)}건에서 상위 3개 종목 합산 비중이 60% 이상이었습니다.",
            "averageReturnPercent": None,
            "averageMaePercent": None,
            "confidence": "high" if len(high) >= 15 else "medium" if len(high) >= 8 else "low",
        })
    if len(concentrations) >= 2:
        change = concentrations[-1] - concentrations[0]
        if abs(change) >= 5:
            direction = "높아졌" if change > 0 else "낮아졌"
            patterns.append({
                "id": "top-holdings-concentration-change",
                "title": "집중도 변화 추세",
                "occurrenceCount": len(concentrations),
                "occurrenceRatePercent": 100.0,
                "description": f"선택 기간의 첫 확정 snapshot 대비 마지막 snapshot의 상위 3개 종목 집중도가 {abs(change):.1f}%p {direction}습니다.",
                "averageReturnPercent": None,
                "averageMaePercent": None,
                "confidence": "high" if len(concentrations) >= 15 else "medium" if len(concentrations) >= 8 else "low",
            })
    if sample < 5:
        headline = f"확정된 거래 후 포트폴리오 snapshot이 {sample}건뿐이라 장기 집중 성향을 단정할 수 없습니다."
    elif high:
        headline = f"확정된 거래 후 snapshot {sample}건 중 {len(high)}건에서 상위 3개 종목 집중도가 60% 이상이었습니다. 수익 여부와 별개로 집중 위험을 장기적으로 점검해야 합니다."
    else:
        headline = f"확정된 거래 후 snapshot {sample}건에서 상위 3개 종목 집중도가 60% 미만으로 유지됐습니다. 집중도와 실제 변동성 기여도를 계속 함께 비교하세요."
    return {"headline": headline, "patterns": patterns, "marketDiversification": diversification}


def _portfolio_market_diversification(
    snapshot: CoachInputSnapshot,
    cutoff: float,
    requested_timestamp: float,
) -> dict[str, Any]:
    """Keep portfolio diversification deterministic and snapshot-bound.

    The snapshot builder may supply stored market correlations and sector-market
    candidates in ``marketContext.portfolioDiversification``.  This function
    never fills absent market facts with an LLM or a generic sector allocation.
    It can still show the actual sector exposure from the latest eligible
    portfolio snapshot.
    """
    history = snapshot.portfolioBefore.get("history")
    rows = [item for item in history if isinstance(item, dict) and (timestamp := _timestamp(item.get("sourceAsOf") or item.get("source_as_of") or item.get("asOf"))) is not None and cutoff <= timestamp <= requested_timestamp] if isinstance(history, list) else []
    latest = max(rows, key=lambda item: _timestamp(item.get("sourceAsOf") or item.get("source_as_of") or item.get("asOf")) or -1, default={})
    positions = latest.get("positions") if isinstance(latest.get("positions"), list) else list(latest.get("positions", {}).values()) if isinstance(latest.get("positions"), dict) else []
    account = latest.get("account") if isinstance(latest.get("account"), dict) else {}
    valued: list[tuple[dict[str, Any], float]] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        value = next((_number(position.get(key)) for key in ("marketValueForeign", "marketValueKrw", "marketValue", "value", "costBasisValue") if _number(position.get(key)) is not None), None)
        if value is not None and value >= 0:
            valued.append((position, value))
    equity = next((_number(account.get(key)) for key in ("totalValueForeign", "totalValueKrw", "equity", "totalEquity", "netAsset") if _number(account.get(key)) is not None), None)
    if equity is None and valued:
        cash = next((_number(account.get(key)) for key in ("cashForeign", "cashKrw", "cash", "availableCash", "cashBalance") if _number(account.get(key)) is not None), 0.0)
        equity = (cash or 0.0) + sum(value for _, value in valued)

    by_sector: dict[str, list[tuple[dict[str, Any], float]]] = {}
    if equity is not None and equity > 0:
        for position, value in valued:
            sector = str(position.get("sector") or "").strip()
            if sector:
                by_sector.setdefault(sector, []).append((position, value))
    exposures = [
        {
            "sector": sector,
            "weightPercent": round(sum(value for _, value in sector_rows) / equity * 100, 4) if equity else None,
            "symbols": sorted({str(position.get("symbol") or "").upper() for position, _ in sector_rows if position.get("symbol")}),
            "riskLevel": "high" if equity and sum(value for _, value in sector_rows) / equity * 100 >= 55 else "attention" if equity and sum(value for _, value in sector_rows) / equity * 100 >= 35 else "normal",
        }
        for sector, sector_rows in by_sector.items()
    ]
    exposures.sort(key=lambda item: (-float(item.get("weightPercent") or 0), str(item["sector"])))
    concentrated = exposures[0] if exposures else None
    source = snapshot.marketContext.get("portfolioDiversification") if isinstance(snapshot.marketContext.get("portfolioDiversification"), dict) else {}
    source_as_of = source.get("sourceAsOf") if isinstance(source, dict) else None
    raw_sensitivities = source.get("holdingSensitivities") if isinstance(source.get("holdingSensitivities"), list) else []
    position_by_symbol = {str(position.get("symbol") or "").upper(): (position, value) for position, value in valued if position.get("symbol")}
    sensitivities: list[dict[str, Any]] = []
    for item in raw_sensitivities:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or symbol not in position_by_symbol:
            continue
        position, value = position_by_symbol[symbol]
        market_correlation = _number(item.get("marketCorrelation"))
        sector_correlation = _number(item.get("sectorCorrelation"))
        independence = "high" if market_correlation is not None and abs(market_correlation) < 0.4 and (sector_correlation is None or abs(sector_correlation) < 0.5) else "low" if (market_correlation is not None and abs(market_correlation) >= 0.7) or (sector_correlation is not None and abs(sector_correlation) >= 0.7) else "unknown"
        sensitivities.append({"symbol": symbol, "sector": position.get("sector"), "weightPercent": round(value / equity * 100, 4) if equity else None, "marketCorrelation": market_correlation, "sectorCorrelation": sector_correlation, "independence": independence})
    sensitivities.sort(key=lambda item: (-float(item.get("weightPercent") or 0), item["symbol"]))

    candidates: list[dict[str, Any]] = []
    raw_candidates = source.get("candidates") if isinstance(source.get("candidates"), list) else []
    if concentrated and float(concentrated.get("weightPercent") or 0) >= 35:
        suggested_max = min(15.0, max(5.0, round((float(concentrated["weightPercent"]) - 35) / 2, 1)))
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            market = str(item.get("market") or "").strip()
            correlation = _number(item.get("correlationToConcentratedSector"))
            relative_strength = _number(item.get("relativeStrengthPercent"))
            role = str(item.get("role") or "")
            if not market or correlation is None or role not in {"defensive", "relative_strength", "diversification"}:
                continue
            candidates.append({
                "id": str(item.get("id") or market), "market": market, "sector": item.get("sector"), "etfSymbol": item.get("etfSymbol"),
                "suggestedMinWeightPercent": _number(item.get("suggestedMinWeightPercent")) if _number(item.get("suggestedMinWeightPercent")) is not None else 5.0,
                "suggestedMaxWeightPercent": _number(item.get("suggestedMaxWeightPercent")) if _number(item.get("suggestedMaxWeightPercent")) is not None else suggested_max,
                "correlationToConcentratedSector": correlation, "relativeStrengthPercent": relative_strength,
                "role": role, "reason": str(item.get("reason") or "현재 집중 섹터와의 상관을 기준으로 계산된 분산 후보입니다."), "sourceAsOf": item.get("sourceAsOf") or source_as_of,
            })
    candidates.sort(key=lambda item: (float(item.get("correlationToConcentratedSector") or 1), -float(item.get("relativeStrengthPercent") or -999), str(item["market"])))
    candidates = candidates[:3]
    missing: list[str] = []
    if not exposures:
        missing.append("현재 포트폴리오의 섹터 분류 또는 평가액이 없습니다.")
    if not raw_sensitivities:
        missing.append("보유 종목의 시장·섹터 상관 데이터가 없습니다.")
    if concentrated and float(concentrated.get("weightPercent") or 0) >= 35 and not candidates:
        missing.append("분산 후보 시장 계산 데이터가 없습니다.")
    return {
        "availability": "ready" if candidates or sensitivities else "insufficient_data",
        "sourceAsOf": source_as_of,
        "concentratedSector": concentrated.get("sector") if concentrated else None,
        "concentratedWeightPercent": concentrated.get("weightPercent") if concentrated else None,
        "sectorExposures": exposures,
        "holdingSensitivities": sensitivities[:8],
        "candidates": candidates,
        "missingData": missing,
    }


def _portfolio_habit_rows(history: Any, cutoff: float, requested_timestamp: float) -> list[dict[str, Any]]:
    """Return one completed, exact state per fill.

    A normal holdings poll is an account observation, not a portfolio decision.
    Counting repeated polls would inflate one unchanged exposure into many habit
    samples.  Only a writer-owned ``fillId`` + ``phase=after`` row proves that the
    state belongs to a completed fill.  Execution mode and valuation basis are
    deliberately orthogonal to that identity boundary.
    """
    if not isinstance(history, list):
        return []

    after_by_fill: dict[str, tuple[float, int, dict[str, Any]]] = {}
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        row_timestamp = _timestamp(item.get("sourceAsOf") or item.get("source_as_of") or item.get("asOf"))
        if row_timestamp is None or not cutoff <= row_timestamp <= requested_timestamp:
            continue

        fill_id = str(item.get("fillId") or item.get("fill_id") or "")
        phase = str(item.get("phase") or "").lower()
        if not fill_id or phase != "after":
            continue
        candidate = (row_timestamp, index, item)
        previous = after_by_fill.get(fill_id)
        if previous is None or candidate[:2] > previous[:2]:
            after_by_fill[fill_id] = candidate

    selected = [(index, item) for _, index, item in after_by_fill.values()]
    selected.sort(key=lambda pair: pair[0])
    return [item for _, item in selected]


def _build_improvement_page(page2: dict[str, Any] | None) -> dict[str, Any] | None:
    if not page2:
        return None
    reports = page2.get("reportsByPeriod", {}).get(page2.get("defaultPeriod", "90d"), {})
    insights = [dict(item) for report in reports.values() if isinstance(report, dict) for item in report.get("insights", []) if isinstance(item, dict)]
    priorities = []
    for item in insights:
        confidence_weight = {"high": 1.0, "medium": 0.8, "low": 0.55, "insufficient": 0.0}.get(str(item.get("confidence")), 0.0)
        score = float(item.get("recurrenceScore") or 0) * float(item.get("impactScore") or 0) * float(item.get("controllabilityScore") or 0) * confidence_weight
        priorities.append({**item, "priorityScore": round(score, 4), "priority": "improve" if item.get("kind") == "improvement_candidate" else "observe"})
    priorities.sort(key=lambda item: (-float(item.get("priorityScore") or 0), str(item.get("id") or "")))
    experiments = [_experiment_for_insight(item) for item in priorities if item.get("priority") == "improve"][:2]
    guardrails = [_guardrail_for_insight(item) for item in priorities if item.get("priority") == "improve"][:3]
    return {"availability": "ready" if priorities else "insufficient_sample", "summary": "반복성과 영향이 확인된 조건부터 다음 거래에서 검증합니다." if priorities else "우선순위를 계산할 충분한 반복 표본이 없습니다.", "priorities": priorities[:6], "experiments": experiments, "guardrails": guardrails}


def _build_action_center(snapshot: CoachInputSnapshot, page1: dict[str, Any] | None, page3: dict[str, Any] | None) -> dict[str, Any]:
    recommended: list[dict[str, Any]] = []
    seen_recommendations: set[str] = set()

    def add_recommendation(candidate: dict[str, Any]) -> None:
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id or not candidate.get("title") or candidate_id in seen_recommendations:
            return
        seen_recommendations.add(candidate_id)
        recommended.append(candidate)

    if page1:
        reviews_by_fill = page1.get("reviewsByFillId")
        if isinstance(reviews_by_fill, dict):
            reviews = [reviews_by_fill[key] for key in sorted(reviews_by_fill) if isinstance(reviews_by_fill[key], dict)]
        else:
            reviews = [page1]
        if not reviews:
            reviews = [page1]
        for review in reviews:
            current_case = review.get("currentCase")
            review_symbol = current_case.get("symbol") if isinstance(current_case, dict) else None
            for condition in review.get("watchConditions", []):
                if not isinstance(condition, dict):
                    continue
                alert_request = condition.get("alertRequest")
                condition_symbol = alert_request.get("symbol") if isinstance(alert_request, dict) else None
                candidate = {
                    "id": condition.get("id"),
                    "symbol": review_symbol or condition_symbol,
                    "title": condition.get("label"),
                    "detail": condition.get("reason"),
                    "currentValue": condition.get("currentValue"),
                    "threshold": condition.get("threshold"),
                    "operator": condition.get("operator"),
                    "recommendedAction": condition.get("recommendedAction"),
                    "alertSupported": bool(condition.get("alertSupported")),
                    "enabled": False,
                    "proposalSource": "daily_trade",
                }
                if condition.get("alertSupported") and isinstance(alert_request, dict):
                    candidate["alertRequest"] = alert_request
                add_recommendation(candidate)

    source_by_stage = {"entry": "entry_habit", "exit": "exit_habit", "portfolio": "portfolio_risk"}
    for priority in (page3 or {}).get("priorities", []):
        if not isinstance(priority, dict):
            continue
        source = source_by_stage.get(str(priority.get("stage") or ""))
        if source is None:
            continue
        detail_parts = [priority.get("condition"), priority.get("observedBehavior"), priority.get("nextAction")]
        add_recommendation({
            "id": f"{source}-{priority.get('id') or 'unknown'}",
            "title": priority.get("title") or priority.get("nextAction"),
            "detail": " · ".join(str(item) for item in detail_parts if item),
            "enabled": False,
            "proposalSource": source,
        })

    watching: list[dict[str, Any]] = []
    for row in snapshot.request.get("alerts", []):
        if isinstance(row, dict):
            candidate = {"id": str(row.get("id")), "title": f"{row.get('symbol')} {row.get('type')}", "detail": str(row.get("target_price") or row.get("change_pct") or ""), "enabled": row.get("status") == "active", "serverAlertId": row.get("id")}
            proposal_source = row.get("proposal_source") or row.get("proposalSource")
            if proposal_source in ALERT_PROPOSAL_SOURCES:
                candidate["proposalSource"] = proposal_source
            watching.append(candidate)
    return {"availability": "ready" if recommended or watching or (page3 and page3.get("experiments")) else "observing", "activeExperiments": [item for item in (page3 or {}).get("experiments", []) if item.get("status") == "active"], "enabledGuardrails": [item for item in (page3 or {}).get("guardrails", []) if item.get("enabled")], "recommendedAlerts": recommended, "watchingAlerts": watching}


def _missed_markers(snapshot: CoachInputSnapshot, fill_id: str) -> list[dict[str, Any]]:
    by_fill = snapshot.request.get("decisionChecksByFillId")
    rows = by_fill.get(fill_id) if isinstance(by_fill, dict) else None
    result: list[dict[str, Any]] = []
    for item in rows if isinstance(rows, list) else []:
        marker = item.get("marker") if isinstance(item, dict) else None
        if item.get("status") == "unchecked" and isinstance(marker, dict):
            result.append(dict(marker))
    return result


def _outcome_series(series: list[dict[str, Any]], source: dict[str, Any]) -> list[dict[str, Any]]:
    end_at = _timestamp(source.get("exitAt") or source.get("evaluationAt"))
    minimum_relative_day = 1 if str(source.get("seriesInterval") or "").lower() in {"1d", "day", "daily"} else 0
    result = []
    for item in series:
        try:
            relative_day = int(item.get("relativeDay", 0))
        except (TypeError, ValueError):
            continue
        # A daily T0 candle includes prices from before an intraday fill. It is
        # valid for chart context, but cannot be used for post-entry MFE/MAE.
        if relative_day < minimum_relative_day:
            continue
        item_at = _timestamp(item.get("time"))
        if end_at is not None and item_at is not None and item_at > end_at:
            continue
        if relative_day <= 20:
            result.append(item)
    return result


def _portfolio_metrics(portfolio: dict[str, Any], symbol: str, sector: str) -> dict[str, float | None]:
    positions = portfolio.get("positions")
    if isinstance(positions, dict):
        positions = list(positions.values())
    rows = [item for item in positions if isinstance(item, dict)] if isinstance(positions, list) else []
    account = portfolio.get("account") if isinstance(portfolio.get("account"), dict) else {}
    valuation_basis = str(portfolio.get("valuationBasis") or "").lower()
    foreign_equity = _number(account.get("totalValueForeign"))
    krw_equity = _number(account.get("totalValueKrw"))
    currency = str(account.get("currency") or "").upper()
    has_foreign_values = any(_number(row.get("marketValueForeign")) is not None for row in rows) or _number(account.get("cashForeign")) is not None
    has_krw_values = any(_number(row.get("marketValueKrw")) is not None for row in rows) or _number(account.get("cashKrw")) is not None
    if valuation_basis == "cost_basis":
        # Paper snapshots intentionally contain acquisition cost, not a live
        # market valuation. Preserve that distinction while comparing the
        # deterministic before/after portfolio state.
        value_keys = ("costBasisValue",)
        cash_keys = ("cashBalance",)
        equity = None
    elif currency == "KRW" and krw_equity is not None:
        value_keys = ("marketValueKrw", "marketValue", "value")
        cash_keys = ("cashKrw", "cash", "availableCash", "cashBalance")
        equity = krw_equity
    elif foreign_equity is not None and has_foreign_values:
        value_keys = ("marketValueForeign", "marketValue", "value")
        cash_keys = ("cashForeign", "cash", "availableCash", "cashBalance")
        equity = foreign_equity
    elif krw_equity is not None and has_krw_values:
        value_keys = ("marketValueKrw", "marketValue", "value")
        cash_keys = ("cashKrw", "cash", "availableCash", "cashBalance")
        equity = krw_equity
    else:
        value_keys = ("marketValueForeign", "marketValueKrw", "marketValue", "value")
        cash_keys = ("cashForeign", "cashKrw", "cash", "availableCash", "cashBalance")
        equity = next((_number(account.get(key)) for key in ("equity", "totalEquity", "netAsset") if _number(account.get(key)) is not None), None)

    values: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        value = next((_number(row.get(key)) for key in value_keys if _number(row.get(key)) is not None), None)
        if value is not None and value >= 0:
            values.append((row, value))
    if rows and not values:
        return {"symbolWeight": None, "sectorWeight": None, "cashWeight": None, "topHoldingsConcentration": None}
    cash = next((_number(account.get(key)) for key in cash_keys if _number(account.get(key)) is not None), None)
    if equity is None and cash is not None and values:
        equity = cash + sum(value for _, value in values)
    if equity is None or equity <= 0:
        return {"symbolWeight": None, "sectorWeight": None, "cashWeight": None, "topHoldingsConcentration": None}
    symbol_value = sum(value for row, value in values if str(row.get("symbol") or "").upper() == symbol.upper())
    sector_classification_complete = bool(sector) and len(values) == len(rows) and all(
        str(row.get("sector") or "").strip()
        for row, _ in values
    )
    sector_value = sum(value for row, value in values if str(row.get("sector") or "") == sector)
    top_three = sum(sorted((value for _, value in values), reverse=True)[:3])
    return {
        "symbolWeight": round(symbol_value / equity * 100, 4),
        "sectorWeight": round(sector_value / equity * 100, 4) if sector_classification_complete else None,
        "cashWeight": round(cash / equity * 100, 4) if cash is not None else None,
        "topHoldingsConcentration": round(top_three / equity * 100, 4),
    }


def _increased(before: dict[str, Any], after: dict[str, Any], key: str) -> bool:
    left = _number(before.get(key)); right = _number(after.get(key))
    return left is not None and right is not None and right > left


def _decreased(before: dict[str, Any], after: dict[str, Any], key: str) -> bool:
    left = _number(before.get(key)); right = _number(after.get(key))
    return left is not None and right is not None and right < left


def _average(values: list[Any]) -> float | None:
    numbers = [float(value) for value in values if _number(value) is not None]
    return round(sum(numbers) / len(numbers), 4) if numbers else None


def _coach_value(value: Any, unit: str) -> dict[str, Any]:
    return {"value": value, "unit": unit, "availability": "ready" if _number(value) is not None else "not_calculated"}


def _experiment_for_insight(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"experiment-{item.get('id')}", "sourceStages": [item.get("stage")], "title": f"{item.get('title')} 확인 절차", "hypothesis": f"{item.get('condition')}을 거래 전에 확인하면 같은 실수를 줄일 수 있습니다.", "sampleTarget": 5, "appliedCount": 0, "checklist": [str(item.get("condition") or "조건 확인"), "진입 전 계획 기록"], "successMetrics": ["계획 준수율", "MAE"], "stopConditions": ["데이터 표본이 왜곡되면 중단"], "confidence": item.get("confidence") or "low", "status": "candidate"}


def _guardrail_for_insight(item: dict[str, Any]) -> dict[str, Any]:
    is_rsi = item.get("id") == "entry-overbought"
    return {"id": f"guardrail-{item.get('id')}", "stage": item.get("stage"), "title": f"{item.get('title')} 재확인", "description": str(item.get("condition") or "조건을 주문 전 확인합니다."), "trigger": {"conditions": [{"metric": "rsi" if is_rsi else "coach_condition", "operator": ">=" if is_rsi else "matches", "value": 70 if is_rsi else str(item.get("id"))}], "matchMode": "all"}, "severity": "warning", "intervention": "require_confirmation", "scope": "next_five_trades", "enabled": False}


def _timestamp(value: Any) -> float | None:
    if not value: return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError): return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool): return None
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None
