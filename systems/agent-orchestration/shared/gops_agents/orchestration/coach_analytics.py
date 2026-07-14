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
        ranked.append(case)
    ranked.sort(key=lambda item: (-float(item["similarityScore"]), str(item["caseId"])))
    return ranked[:6]


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
    if holding_duration is None:
        holding_duration = max((int(item.get("relativeDay", 0)) for item in outcome_series), default=0)
    return {
        "caseId": str(source.get("caseId") or source.get("fillId") or ("current" if current else "unknown")),
        "tradeDate": source.get("tradeDate") or source.get("filledAt"), "symbol": source.get("symbol"),
        "side": source.get("side"), "entryPrice": entry, "exitPrice": source.get("exitPrice"),
        **outcomes, "holdingDuration": holding_duration, "series": series,
        "missedChecks": list(source.get("missedChecks") or []),
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
    if not checks:
        grade = "insufficient_data"; process = "확인 기록 없음"; summary = "손익과 별개로 판단 절차를 평가할 확인 기록이 없습니다."
    elif len(missed) >= 2:
        grade = "risk"; process = f"{len(missed)}개 핵심 조건 미확인"; summary = "여러 핵심 확인 절차를 건너뛴 판단이었습니다."
    elif missed:
        grade = "attention"; process = f"{missed[0].get('label') or '핵심 조건'} 미확인"; summary = "결과와 무관하게 보완해야 할 확인 절차가 있습니다."
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
            conditions.append({"id": f"{case.get('caseId')}-{suffix}", "type": "price", "label": label, "currentValue": latest.get("close"), "threshold": round(threshold, 4), "operator": operator, "reason": "최근 20개 일봉 범위의 검증된 가격 수준", "recommendedAction": action, "alertSupported": True, "alertRequest": {"symbol": symbol, "type": "price_cross", "targetPrice": str(round(threshold, 4)), "repeatLimit": 1}})
    if latest.get("relativeVolume") is not None:
        conditions.append({"id": f"{case.get('caseId')}-volume", "type": "volume", "label": "상대 거래량 1.2 회복", "currentValue": latest.get("relativeVolume"), "threshold": 1.2, "operator": ">=", "reason": "참여 강도 회복 확인", "recommendedAction": "재관찰", "alertSupported": False})
    if latest.get("rsi") is not None:
        conditions.append({"id": f"{case.get('caseId')}-rsi", "type": "rsi", "label": "RSI 과열 구간 이탈", "currentValue": latest.get("rsi"), "threshold": 70, "operator": "<", "reason": "과열 완화 여부 확인", "recommendedAction": "추세 재평가", "alertSupported": False})
    return conditions


def _confidence(snapshot: CoachInputSnapshot, fill_id: str) -> dict[str, Any]:
    by_fill = snapshot.request.get("decisionChecksByFillId")
    checks = by_fill.get(fill_id) if isinstance(by_fill, dict) else None
    available = len(checks) if isinstance(checks, list) else 0
    return {"level": "medium" if available >= 4 else "low", "reason": f"확인 기록 {available}건" if available else "확인 기록 없음"}


def _build_habit_page(snapshot: CoachInputSnapshot) -> dict[str, Any] | None:
    rows = snapshot.chartContext.get("historicalCases")
    cases = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    requested_at = datetime.fromtimestamp(_timestamp(snapshot.request.get("requestedAt")) or datetime.now(timezone.utc).timestamp(), tz=timezone.utc)
    requested_timestamp = requested_at.timestamp()
    reports_by_period: dict[str, Any] = {}
    for key, days in (("30d", 30), ("90d", 90), ("1y", 365)):
        cutoff = requested_at.timestamp() - days * 86400
        period_cases = [
            item
            for item in cases
            if (case_timestamp := _timestamp(item.get("tradeDate") or item.get("filledAt"))) is not None
            and cutoff <= case_timestamp <= requested_timestamp
        ]
        reports_by_period[key] = {
            "entry": _habit_report("entry", [item for item in period_cases if str(item.get("side")).lower() == "buy"], f"최근 {days}일"),
            "exit": _habit_report("exit", [item for item in period_cases if str(item.get("side")).lower() == "sell"], f"최근 {days}일"),
            "portfolio": _portfolio_habit_report(snapshot, period_cases, f"최근 {days}일", cutoff, requested_timestamp),
        }
    has_samples = any(report.get("sampleSize", 0) for reports in reports_by_period.values() for report in reports.values())
    return {"availability": "ready" if has_samples else "insufficient_sample", "defaultPeriod": "90d", "reportsByPeriod": reports_by_period}


def _habit_report(stage: str, cases: list[dict[str, Any]], period_label: str) -> dict[str, Any]:
    outcomes = [_trade_case(item, item, current=False) for item in cases]
    returns = [float(item["returnPercent"]) for item in outcomes if _number(item.get("returnPercent")) is not None]
    mfes = [float(item["mfePercent"]) for item in outcomes if _number(item.get("mfePercent")) is not None]
    maes = [float(item["maePercent"]) for item in outcomes if _number(item.get("maePercent")) is not None]
    sample = len(cases); confidence = "high" if sample >= 15 else "low" if sample >= 5 else "insufficient"
    insights: list[dict[str, Any]] = []
    overbought = [item for item in cases if item.get("rsiBand") == "overbought"]
    if stage == "entry" and len(overbought) >= 5:
        over_outcomes = [_trade_case(item, item, current=False) for item in overbought]
        avg_return = _average([item.get("returnPercent") for item in over_outcomes])
        insights.append({"id": "entry-overbought", "stage": "entry", "kind": "improvement_candidate" if (avg_return or 0) < 0 else "observation", "title": "RSI 과열 구간 진입", "condition": "진입 시 RSI 70 이상", "observedBehavior": "과열 구간 진입의 사후 성과를 같은 기준으로 집계했습니다.", "sampleSize": len(overbought), "confidence": "low" if len(overbought) < 15 else "high", "metrics": {"avgReturn": avg_return}, "recurrenceScore": min(1.0, len(overbought) / max(1, sample)), "impactScore": min(1.0, abs(avg_return or 0) / 10), "controllabilityScore": 0.9})
    availability = "ready" if sample >= 5 else "insufficient_sample"
    return {"stage": stage, "availability": availability, "periodLabel": period_label, "sampleSize": sample, "confidence": confidence, "missingData": [] if sample else ["표본 부족"], "summary": f"{sample}건의 {('진입' if stage == 'entry' else '청산')} 거래를 결과와 판단 절차로 분리해 집계했습니다." if sample else "분석할 거래 표본이 없습니다.", "behavior": [{"label": "평균 수익률", "value": _coach_value(_average(returns), "%")}, {"label": "평균 MFE", "value": _coach_value(_average(mfes), "%")}, {"label": "평균 MAE", "value": _coach_value(_average(maes), "%")}], "planConsistency": {"availability": "no_confirmation_record"}, "insights": insights}


def _portfolio_habit_report(
    snapshot: CoachInputSnapshot,
    cases: list[dict[str, Any]],
    period_label: str,
    cutoff: float,
    requested_timestamp: float,
) -> dict[str, Any]:
    history = snapshot.portfolioBefore.get("history")
    rows = [
        item
        for item in history
        if isinstance(item, dict)
        and (row_timestamp := _timestamp(item.get("sourceAsOf") or item.get("source_as_of") or item.get("asOf"))) is not None
        and cutoff <= row_timestamp <= requested_timestamp
    ] if isinstance(history, list) else []
    sample = len(rows)
    concentrations = [_portfolio_metrics(row, "", "").get("topHoldingsConcentration") for row in rows]
    valid = [float(value) for value in concentrations if _number(value) is not None]
    insights: list[dict[str, Any]] = []
    high_count = len([value for value in valid if value >= 60])
    if high_count >= 5:
        insights.append({"id": "portfolio-concentration", "stage": "portfolio", "kind": "improvement_candidate", "title": "상위 종목 집중", "condition": "상위 3개 종목 합산 비중 60% 이상", "observedBehavior": "높은 집중 상태가 반복되었습니다.", "sampleSize": high_count, "confidence": "low" if high_count < 15 else "high", "metrics": {"riskContribution": _average(valid)}, "recurrenceScore": high_count / max(1, sample), "impactScore": min(1.0, (_average(valid) or 0) / 100), "controllabilityScore": 0.8})
    return {"stage": "portfolio", "availability": "ready" if sample >= 5 else "insufficient_sample", "periodLabel": period_label, "sampleSize": sample, "confidence": "high" if sample >= 15 else "low" if sample >= 5 else "insufficient", "missingData": [] if sample else ["포트폴리오 이력 부족"], "summary": f"{sample}개 포트폴리오 snapshot의 집중도를 집계했습니다." if sample else "포트폴리오 이력이 부족합니다.", "behavior": [{"label": "평균 상위 종목 집중도", "value": _coach_value(_average(valid), "%")}], "planConsistency": {"availability": "no_confirmation_record"}, "insights": insights}


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
    recommended = []
    if page1:
        for condition in page1.get("watchConditions", []):
            if condition.get("alertSupported") and isinstance(condition.get("alertRequest"), dict):
                recommended.append({"id": condition.get("id"), "title": condition.get("label"), "detail": condition.get("reason"), "enabled": False, "alertRequest": condition.get("alertRequest")})
    watching = []
    for row in snapshot.request.get("alerts", []):
        if isinstance(row, dict):
            watching.append({"id": str(row.get("id")), "title": f"{row.get('symbol')} {row.get('type')}", "detail": str(row.get("target_price") or row.get("change_pct") or ""), "enabled": row.get("status") == "active", "serverAlertId": row.get("id")})
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
    foreign_equity = _number(account.get("totalValueForeign"))
    krw_equity = _number(account.get("totalValueKrw"))
    currency = str(account.get("currency") or "").upper()
    has_foreign_values = any(_number(row.get("marketValueForeign")) is not None for row in rows) or _number(account.get("cashForeign")) is not None
    has_krw_values = any(_number(row.get("marketValueKrw")) is not None for row in rows) or _number(account.get("cashKrw")) is not None
    if currency == "KRW" and krw_equity is not None:
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
    sector_value = sum(value for row, value in values if sector and str(row.get("sector") or "") == sector)
    top_three = sum(sorted((value for _, value in values), reverse=True)[:3])
    return {
        "symbolWeight": round(symbol_value / equity * 100, 4),
        "sectorWeight": round(sector_value / equity * 100, 4) if sector else None,
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
