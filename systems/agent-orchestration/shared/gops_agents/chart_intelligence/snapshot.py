from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable

from ..contracts import EvidenceItem, FinalAnswer, FinalAnswerSection, utc_now_iso


PATTERN_LABELS = {
    "ascending_triangle": "상승 삼각형", "descending_triangle": "하락 삼각형", "symmetrical_triangle": "대칭 삼각형",
    "bullish_flag": "상승 깃발형", "bearish_flag": "하락 깃발형", "bullish_pennant": "상승 페넌트",
    "bearish_pennant": "하락 페넌트", "bullish_rectangle": "상승 직사각형", "bearish_rectangle": "하락 직사각형",
    "rising_wedge": "상승 쐐기", "falling_wedge": "하락 쐐기", "descending_channel_breakout": "하락 채널 상단 돌파",
    "ascending_channel_breakdown": "상승 채널 하단 이탈",
}
STATE_LABELS = {"forming": "형성 중", "confirmed": "돌파 확인", "inactive": "비활성", "invalidated": "무효화"}
ACTION_LABELS = {
    "watch": "관찰 후보", "buy_candidate": "조건부 매수 검토", "sell_candidate": "조건부 매도 검토",
    "no_trade": "진입 보류",
}
REASON_LABELS = {
    "confirmed_upward_breakout": "상단 돌파가 확인됨", "confirmed_downward_breakout": "하단 이탈이 확인됨",
    "long_position_exit_only": "기존 보유분 매도 검토 시나리오", "pattern_not_confirmed": "패턴 확인 전",
    "pattern_not_active": "패턴이 활성 상태가 아님", "reward_risk_passed": "최소 손익비 기준 충족",
    "reward_risk_below_minimum": "최소 손익비 기준 미달", "breakout_direction_mismatch": "예상 방향과 실제 돌파 방향이 다름",
    "confirmed_state_without_current_breakout": "현재 구간에서 돌파 봉을 재확인하지 못함",
    "missing_pattern_boundaries": "패턴 경계선이 부족함",
}


def _apply_shared_semantic_catalog() -> None:
    catalog_path = Path(__file__).resolve().parents[5] / "shared" / "chart-contract" / "chart-semantics.ko.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    for target, key in (
        (PATTERN_LABELS, "patterns"), (STATE_LABELS, "states"),
        (ACTION_LABELS, "actions"), (REASON_LABELS, "reasons"),
    ):
        values = catalog.get(key)
        if isinstance(values, dict):
            target.update({str(code): str(label) for code, label in values.items()})


_apply_shared_semantic_catalog()


def build_chart_explanation(context: Any, asset: dict[str, Any] | None) -> dict[str, Any]:
    chart_context = getattr(context, "chartContext", {}) if isinstance(getattr(context, "chartContext", {}), dict) else {}
    chart_document = chart_context.get("chartDocument") if isinstance(chart_context.get("chartDocument"), dict) else {}
    symbol = str((asset or {}).get("symbol") or chart_document.get("symbol") or getattr(context, "symbol", "UNKNOWN")).upper()
    interval = str((asset or {}).get("interval") or chart_document.get("timeframe") or "unknown")
    candles = _candles(chart_context)
    reference = _selected_chart_reference(context)
    selected = _selected_candle_features(reference, candles)
    geometry = (asset or {}).get("geometry") if isinstance((asset or {}).get("geometry"), dict) else {}
    pattern = geometry.get("primaryPattern") or geometry.get("primaryTriangle")
    pattern = pattern if isinstance(pattern, dict) else None
    trade_plan = geometry.get("tradePlan") if isinstance(geometry.get("tradePlan"), dict) else None
    current_price = _number(selected.get("close")) if selected else None
    if current_price is None and candles:
        current_price = _number(candles[-1].get("close"))
    if current_price is None:
        visible = chart_context.get("visibleSummary") if isinstance(chart_context.get("visibleSummary"), dict) else {}
        current_price = _number(visible.get("lastPrice"))
    supports = _nearest_levels(geometry.get("supports"), current_price)
    resistances = _nearest_levels(geometry.get("resistances"), current_price)
    indicators = (asset or {}).get("indicators") if isinstance((asset or {}).get("indicators"), dict) else {}
    coverage = (asset or {}).get("coverage") if isinstance((asset or {}).get("coverage"), dict) else {}
    quality_state = str(coverage.get("state") or ("screen_only" if candles else "unavailable"))
    quality_flags = [str(item) for item in coverage.get("qualityFlags", []) if str(item)]
    drawing_ids = list(dict.fromkeys(
        str(item.get("id"))
        for item in geometry.get("drawings", [])
        if isinstance(item, dict) and item.get("id")
    ))
    pattern_fact = None
    if pattern:
        pattern_fact = {
            "id": str(pattern.get("id") or pattern.get("geometryHash") or pattern.get("kind") or "pattern"),
            "kind": str(pattern.get("kind") or "unknown"),
            "label": PATTERN_LABELS.get(str(pattern.get("kind")), str(pattern.get("kind") or "패턴")),
            "state": str(pattern.get("state") or "unknown"),
            "stateLabel": STATE_LABELS.get(str(pattern.get("state")), str(pattern.get("state") or "상태 미확인")),
            "bias": pattern.get("bias"),
            "breakoutDirection": pattern.get("breakoutDirection"),
            "score": _number(pattern.get("score")),
            "touches": int(pattern.get("touches") or 0),
            "confirmation": dict(pattern.get("confirmation")) if isinstance(pattern.get("confirmation"), dict) else None,
        }
    scenario = _trade_scenario(trade_plan)
    cross = _cross_fact(indicators.get("cross"))
    facts = {
        "pattern": pattern_fact,
        "support": supports[0] if supports else None,
        "resistance": resistances[0] if resistances else None,
        "tradeScenario": scenario,
        "movingAverageCross": cross,
        "selectedCandle": selected,
    }
    if "primaryTrend" in geometry:
        primary_trend = geometry.get("primaryTrend")
        facts["trend"] = dict(primary_trend) if isinstance(primary_trend, dict) else None
    used_indicators = ["가격 구조", "지지·저항"]
    if indicators.get("sma60") is not None or indicators.get("sma120") is not None:
        used_indicators.append("SMA60/120")
    if selected and selected.get("atr") is not None:
        used_indicators.append("ATR(14)")
    if selected and selected.get("relativeVolume") is not None:
        used_indicators.append("20봉 상대 거래량")
    source = {
        key: str(chart_document[key])
        for key in ("chartDocumentId", "sourcePanelId")
        if chart_document.get(key)
    }
    explanation = {
        "version": "chart-explanation.v1",
        "symbol": symbol,
        "interval": interval,
        "asOf": (asset or {}).get("asOf") or (candles[-1].get("timestamp") if candles else utc_now_iso()),
        "quality": {"state": quality_state, "stale": "stale" in quality_flags, "flags": quality_flags},
        "assetIdentity": {
            "assetVersion": (asset or {}).get("assetVersion"), "algorithmVersion": (asset or {}).get("algorithmVersion"),
            "inputDigest": (asset or {}).get("inputDigest"), "asOf": (asset or {}).get("asOf"),
        },
        "facts": facts,
        "usedIndicators": used_indicators,
        "focusIds": drawing_ids,
        "focusGroups": _focus_groups(geometry, drawing_ids, pattern),
        "anchor": _reference_anchor(reference),
        "news": [],
    }
    if source:
        explanation["source"] = source
    return validate_chart_explanation_contract(explanation)


def validate_chart_explanation_contract(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the Python producer boundary without adding a runtime schema package."""
    required = {
        "version", "symbol", "interval", "asOf", "quality", "assetIdentity",
        "facts", "usedIndicators", "focusIds", "anchor", "news",
    }
    missing = sorted(required.difference(value))
    if missing:
        raise ValueError(f"chart explanation is missing required fields: {', '.join(missing)}")
    if value.get("version") != "chart-explanation.v1":
        raise ValueError("unsupported chart explanation version")
    if not all(isinstance(value.get(key), str) and str(value[key]).strip() for key in ("symbol", "interval", "asOf")):
        raise ValueError("chart explanation identity is invalid")
    if not isinstance(value.get("quality"), dict) or not isinstance(value.get("assetIdentity"), dict) or not isinstance(value.get("facts"), dict):
        raise ValueError("chart explanation object fields are invalid")
    focus_ids = value.get("focusIds")
    if not isinstance(focus_ids, list) or not all(isinstance(item, str) and item for item in focus_ids):
        raise ValueError("chart explanation focusIds are invalid")
    focus_groups = value.get("focusGroups")
    if focus_groups is not None:
        if not isinstance(focus_groups, dict):
            raise ValueError("chart explanation focusGroups are invalid")
        allowed_ids = set(focus_ids)
        allowed_group_keys = {"evidence", "levels", "trend", "pattern", "support", "resistance"}
        if any(key not in allowed_group_keys for key in focus_groups):
            raise ValueError("chart explanation focusGroups contain unsupported groups")
        for key in ("evidence", "pattern", "support", "resistance"):
            ids = focus_groups.get(key)
            if not isinstance(ids, list) or not all(isinstance(item, str) and item in allowed_ids for item in ids):
                raise ValueError(f"chart explanation focusGroups.{key} is invalid")
        for key in ("levels", "trend"):
            ids = focus_groups.get(key)
            if ids is not None and (
                not isinstance(ids, list)
                or not all(isinstance(item, str) and item in allowed_ids for item in ids)
            ):
                raise ValueError(f"chart explanation focusGroups.{key} is invalid")
    source = value.get("source")
    if source is not None and (
        not isinstance(source, dict)
        or not source
        or any(key not in {"chartDocumentId", "sourcePanelId"} for key in source)
        or not all(isinstance(item, str) and item for item in source.values())
    ):
        raise ValueError("chart explanation source is invalid")
    return value


def chart_explanation_evidence(explanation: dict[str, Any]) -> EvidenceItem:
    facts = explanation.get("facts") if isinstance(explanation.get("facts"), dict) else {}
    pattern = facts.get("pattern") if isinstance(facts.get("pattern"), dict) else None
    summary = f"{explanation.get('symbol')} {explanation.get('interval')} 차트 구조를 분석했습니다."
    if pattern:
        summary = f"{pattern.get('label')} · {pattern.get('stateLabel')} 구조를 확인했습니다."
    return EvidenceItem(
        provider="chart-analysis", status="available", title="Chart explanation", summary=summary,
        observedAt=str(explanation.get("asOf") or utc_now_iso()), raw={"chartExplanation": explanation, "sourceType": "chart_analysis_snapshot"},
    )


def chart_explanation_from_evidence(items: Iterable[EvidenceItem]) -> dict[str, Any] | None:
    for item in items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        value = raw.get("chartExplanation")
        if item.provider == "chart-analysis" and isinstance(value, dict):
            return value
    return None


def build_chart_final_answer(symbol: str, explanation: dict[str, Any], *, reference_mode: bool = False) -> FinalAnswer:
    facts = explanation.get("facts") if isinstance(explanation.get("facts"), dict) else {}
    pattern = facts.get("pattern") if isinstance(facts.get("pattern"), dict) else None
    selected = facts.get("selectedCandle") if isinstance(facts.get("selectedCandle"), dict) else None
    scenario = facts.get("tradeScenario") if isinstance(facts.get("tradeScenario"), dict) else None
    support = facts.get("support") if isinstance(facts.get("support"), dict) else None
    resistance = facts.get("resistance") if isinstance(facts.get("resistance"), dict) else None
    if reference_mode and selected:
        summary = _selected_summary(symbol, explanation.get("interval"), selected, pattern)
        observations = _selected_bullets(selected)
        title = f"{symbol} 선택 봉 분석"
    else:
        summary = _overview_summary(symbol, explanation.get("interval"), pattern, scenario)
        observations = _overview_bullets(pattern, support, resistance)
        title = f"{symbol} 차트 해설"
    sections = [FinalAnswerSection(title="주요 관찰", bullets=observations or ["현재 화면 구간의 가격 구조를 기준으로 분석했습니다."])]
    scenario_bullets = _scenario_bullets(scenario)
    if scenario_bullets:
        sections.append(FinalAnswerSection(title="확인·재검토 조건", bullets=scenario_bullets))
    indicators = [str(item) for item in explanation.get("usedIndicators", []) if str(item)]
    if indicators:
        sections.append(FinalAnswerSection(title="분석한 지표", bullets=[", ".join(indicators)]))
    limitations = []
    quality = explanation.get("quality") if isinstance(explanation.get("quality"), dict) else {}
    if quality.get("state") in {"partial", "screen_only"}:
        limitations.append("일부 데이터만 사용할 수 있어 화면에 포함된 구간 기준으로 해설했습니다.")
    return FinalAnswer(title=title, summary=summary, sections=sections[:3], citations=[], limitations=limitations)


def _overview_summary(symbol: str, interval: Any, pattern: dict[str, Any] | None, scenario: dict[str, Any] | None) -> str:
    if pattern:
        text = f"{symbol} {interval} 차트는 {pattern['label']}이(가) {pattern['stateLabel']} 상태입니다."
        if scenario:
            text += f" 현재 해석은 {scenario['actionLabel']} 시나리오입니다."
        return text
    return f"{symbol} {interval} 차트에서 확인 가능한 지지·저항과 이동평균 구조를 정리했습니다."


def _selected_summary(symbol: str, interval: Any, selected: dict[str, Any], pattern: dict[str, Any] | None) -> str:
    direction = "상승" if float(selected.get("close", 0)) > float(selected.get("open", 0)) else "하락" if float(selected.get("close", 0)) < float(selected.get("open", 0)) else "보합"
    suffix = f" 같은 시점의 {pattern['label']} 구조 안에 있습니다." if pattern else " 해당 시점의 가격 구조를 기준으로 해석했습니다."
    return f"{symbol} {interval} {selected.get('timestamp')} 봉은 {direction} 봉입니다.{suffix}"


def _overview_bullets(pattern: dict[str, Any] | None, support: dict[str, Any] | None, resistance: dict[str, Any] | None) -> list[str]:
    bullets = []
    if pattern:
        score = pattern.get("score")
        score_text = f" (패턴 점수 {float(score):.0%})" if isinstance(score, (int, float)) else ""
        bullets.append(f"{pattern['label']} · {pattern['stateLabel']}{score_text}, 접촉 {pattern.get('touches', 0)}회")
    if support:
        bullets.append(f"가까운 지지 가격은 {_price(support.get('price'))}입니다.")
    if resistance:
        bullets.append(f"가까운 저항 가격은 {_price(resistance.get('price'))}입니다.")
    return bullets


def _selected_bullets(selected: dict[str, Any]) -> list[str]:
    bullets = [f"시가 {_price(selected.get('open'))}, 고가 {_price(selected.get('high'))}, 저가 {_price(selected.get('low'))}, 종가 {_price(selected.get('close'))}"]
    if selected.get("rangeAtr") is not None:
        bullets.append(f"봉 전체 범위는 ATR의 {float(selected['rangeAtr']):.2f}배, 몸통 비중은 {float(selected.get('bodyRatio') or 0):.0%}입니다.")
    if selected.get("relativeVolume") is not None:
        bullets.append(f"거래량은 직전 20봉 중앙값의 {float(selected['relativeVolume']):.2f}배입니다.")
    if selected.get("gapPercent") is not None:
        bullets.append(f"직전 종가 대비 갭은 {float(selected['gapPercent']):+.2f}%입니다.")
    if selected.get("previousToSelectedPercent") is not None:
        bullets.append(f"직전 완료 봉 종가에서 선택 봉 종가까지 {float(selected['previousToSelectedPercent']):+.2f}% 움직였습니다.")
    if selected.get("selectedToNextPercent") is not None:
        bullets.append(f"선택 봉 종가에서 다음 완료 봉 종가까지 {float(selected['selectedToNextPercent']):+.2f}% 반응했습니다.")
    return bullets


def _scenario_bullets(scenario: dict[str, Any] | None) -> list[str]:
    if not scenario:
        return []
    bullets = [f"{scenario['actionLabel']}: {', '.join(scenario.get('reasonLabels') or [])}"]
    if scenario.get("signalAt"):
        bullets.append(f"신호 확인 시점은 {scenario['signalAt']}입니다.")
    prices = []
    sell = scenario.get("action") == "sell_candidate"
    price_fields = (
        (("매도", "entryPrice"), ("재검토", "stopPrice"), ("예상 하단", "targetPrice"))
        if sell else (("진입", "entryPrice"), ("손절", "stopPrice"), ("목표", "targetPrice"))
    )
    for label, key in price_fields:
        if scenario.get(key) is not None:
            prices.append(f"{label} {_price(scenario[key])}")
    if prices:
        rr = f", 손익비 {float(scenario['rewardRiskRatio']):.2f}" if scenario.get("rewardRiskRatio") is not None else ""
        bullets.append(" / ".join(prices) + rr)
    return bullets


def _trade_scenario(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not plan:
        return None
    action = str(plan.get("action") or "no_trade")
    reasons = [str(item) for item in plan.get("reasons", [])]
    return {
        **{key: plan.get(key) for key in ("action", "direction", "signalAt", "entryTrigger", "entryPrice", "stopPrice", "targetPrice", "rewardRiskRatio")},
        "actionLabel": ACTION_LABELS.get(action, action),
        "reasonLabels": [REASON_LABELS.get(item, item.replace("_", " ")) for item in reasons],
        "positionMeaning": "기존 보유분 매도 검토 시나리오" if action == "sell_candidate" and plan.get("direction") == "exit_long" else None,
    }


def _cross_fact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("status") != "crossed":
        return None
    return {
        "direction": value.get("direction"), "label": "골든크로스" if value.get("direction") == "golden" else "데드크로스",
        "previousTimestamp": value.get("previousTimestamp"), "confirmedAt": value.get("timestamp"),
        "fraction": value.get("fraction"), "price": value.get("price"),
    }


def _selected_chart_reference(context: Any) -> dict[str, Any] | None:
    for item in getattr(context, "references", []) or []:
        if isinstance(item, dict) and str(item.get("type") or "").startswith("chart."):
            return item
    return None


def _reference_anchor(reference: dict[str, Any] | None) -> dict[str, Any] | None:
    if not reference:
        return None
    data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
    return {"type": reference.get("type"), "sourcePanelId": reference.get("sourcePanelId"), "id": data.get("id"), "timestamp": data.get("timestamp") or data.get("from")}


def _selected_candle_features(reference: dict[str, Any] | None, candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not reference or reference.get("type") != "chart.candle":
        return None
    data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
    timestamp = str(data.get("timestamp") or data.get("from") or "")
    index = next((i for i, row in enumerate(candles) if str(row.get("timestamp")) == timestamp), -1)
    row = dict(candles[index]) if index >= 0 else dict(data)
    required = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in required):
        return None
    open_price, high, low, close = (float(value) for value in required if value is not None)
    candle_range = max(high - low, 0.0)
    previous = candles[index - 1] if index > 0 else None
    following = candles[index + 1] if 0 <= index < len(candles) - 1 else None
    atr = _atr(candles[: index + 1]) if index >= 0 else None
    baseline = [_number(item.get("volume")) for item in candles[max(0, index - 20):index]] if index > 0 else []
    volumes = [float(item) for item in baseline if item is not None and item >= 0]
    volume = _number(row.get("volume"))
    median_volume = statistics.median(volumes) if volumes else None
    return {
        "timestamp": timestamp or row.get("timestamp"), "open": open_price, "high": high, "low": low, "close": close, "volume": volume,
        "body": abs(close - open_price), "bodyRatio": abs(close - open_price) / candle_range if candle_range > 0 else 0.0,
        "upperWick": high - max(open_price, close), "lowerWick": min(open_price, close) - low,
        "atr": atr, "rangeAtr": candle_range / atr if atr and atr > 0 else None,
        "relativeVolume": volume / median_volume if volume is not None and median_volume and median_volume > 0 else None,
        "gapPercent": ((open_price / float(previous["close"])) - 1) * 100 if previous and _number(previous.get("close")) else None,
        "previousToSelectedPercent": ((close / float(previous["close"])) - 1) * 100 if previous and _number(previous.get("close")) else None,
        "selectedToNextPercent": ((float(following["close"]) / close) - 1) * 100 if following and _number(following.get("close")) and close else None,
    }


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(candles) < 2:
        return None
    ranges = []
    offset = max(0, len(candles) - period)
    for index, row in enumerate(candles[-period:]):
        high, low = _number(row.get("high")), _number(row.get("low"))
        absolute_index = offset + index
        previous_close = _number(candles[absolute_index - 1].get("close")) if absolute_index > 0 else _number(row.get("close"))
        if high is None or low is None or previous_close is None:
            continue
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return statistics.mean(ranges) if ranges else None


def _nearest_levels(values: Any, current_price: float | None) -> list[dict[str, Any]]:
    levels = [dict(item) for item in values or [] if isinstance(item, dict) and _number(item.get("price")) is not None]
    if current_price is None:
        return levels[:2]
    return sorted(levels, key=lambda item: abs(float(item["price"]) - current_price))[:2]


def _focus_groups(
    geometry: dict[str, Any],
    drawing_ids: list[str],
    primary_pattern: dict[str, Any] | None,
) -> dict[str, list[str]]:
    def level_ids(values: Any) -> list[str]:
        raw_ids = {
            str(item.get("id"))
            for item in values or []
            if isinstance(item, dict) and item.get("id")
        }
        return [
            drawing_id for drawing_id in drawing_ids
            if any(drawing_id == level_id or drawing_id.endswith(f":{level_id}") for level_id in raw_ids)
        ]

    geometry_hash = str((primary_pattern or {}).get("geometryHash") or "")
    result = {
        "evidence": list(drawing_ids),
        "pattern": [drawing_id for drawing_id in drawing_ids if geometry_hash and geometry_hash in drawing_id],
        "support": level_ids(geometry.get("supports")),
        "resistance": level_ids(geometry.get("resistances")),
    }
    drawing_groups = geometry.get("drawingGroups")
    if isinstance(drawing_groups, dict):
        def stored_group(name: str) -> list[str]:
            requested = {
                str(item)
                for item in drawing_groups.get(name) or []
                if isinstance(item, str) and item
            }
            return [drawing_id for drawing_id in drawing_ids if drawing_id in requested]

        result["levels"] = stored_group("levels")
        result["trend"] = stored_group("trend")
        result["pattern"] = stored_group("pattern")
    return result


def _candles(chart_context: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in chart_context.get("candles", []) if isinstance(item, dict) and item.get("timestamp")]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price(value: Any) -> str:
    number = _number(value)
    return "확인 불가" if number is None else f"{number:,.2f}"
