from __future__ import annotations

import hashlib
from typing import Any


STYLE_TOKENS: dict[str, dict[str, Any]] = {
    "asset-sr-strong": {"color": "#f5f5f5", "colorToken": "asset-sr-strong", "lineWidth": 2, "opacity": 0.95},
    "asset-sr-medium": {"color": "#f5f5f5", "colorToken": "asset-sr-medium", "lineWidth": 1.5, "opacity": 0.75},
    "asset-sr-weak": {"color": "#f5f5f5", "colorToken": "asset-sr-weak", "lineWidth": 1, "opacity": 0.55},
    "asset-flag": {"color": "#ff7a3d", "colorToken": "asset-flag", "lineWidth": 1, "opacity": 0.95},
    "asset-trend": {"color": "#0099ff", "colorToken": "asset-trend", "lineWidth": 1.5, "opacity": 0.9, "extension": "ray"},
    "asset-range": {"color": "#999999", "colorToken": "asset-range", "lineWidth": 1, "fillColor": "#999999", "fillToken": "asset-range", "fillOpacity": 0.06, "opacity": 0.8},
    "asset-pattern-bull": {"color": "#22c55e", "colorToken": "asset-pattern-bull", "lineWidth": 2, "opacity": 0.95, "extension": "ray"},
    "asset-pattern-bear": {"color": "#ef4444", "colorToken": "asset-pattern-bear", "lineWidth": 2, "opacity": 0.95, "extension": "ray"},
    "asset-pattern-neutral": {"color": "#f59e0b", "colorToken": "asset-pattern-neutral", "lineWidth": 2, "opacity": 0.95, "extension": "ray"},
}


def recommended_indicators(features: dict[str, Any]) -> list[dict[str, str]]:
    regime = features.get("regime") or {}
    candidates: list[dict[str, str]] = []
    if regime.get("bbSqueeze") or float(regime.get("bbBandwidthPercentile") or 1) < 0.2:
        candidates.append({"layer": "bollinger:20:2", "reason": "변동성 수축 — 확장 임박 관찰", "source": "rule"})
    macd_state = str(regime.get("macdState") or "neutral")
    if macd_state in {"bullish_cross_recent", "bearish_cross_recent", "diverging"}:
        candidates.append({"layer": "macd:12:26:9", "reason": "MACD 크로스/다이버전스 후보", "source": "rule"})
    rsi_value = float(regime.get("rsi14") or 50)
    if rsi_value >= 70 or rsi_value <= 30:
        candidates.append({"layer": "rsi:14", "reason": "과열/과매도 구간", "source": "rule"})
    return candidates[:2]


def compile_rule_layers(
    *,
    symbol: str,
    interval: str,
    features: dict[str, Any],
    candles: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """Compile only kernel-approved geometry into the two deterministic layers."""
    if not candles:
        return {
            "structure": _empty_layer("no_candles"),
            "trend": _empty_layer("no_candles"),
        }

    candle_times = {item["timestamp"] for item in candles}
    current = float(candles[-1]["close"])
    all_levels = list(features.get("levels", []))
    # Gap-up/down markers were retired in kernel-v6. Keep the compiler guard so
    # legacy feature payloads cannot reintroduce them as rule drawings.
    all_events = [item for item in features.get("events", []) if item.get("kind") != "gap"]
    levels = [
        item for item in all_levels
        if item.get("hardPass") and item.get("role") in {"support", "resistance"}
    ]
    supports = sorted((item for item in levels if float(item["price"]) <= current), key=_level_rank)
    resistances = sorted((item for item in levels if float(item["price"]) > current), key=_level_rank)
    chosen = (supports[:1] + resistances[:1])[:2]

    structure_drawings: list[dict[str, Any]] = []
    structure_selected: list[dict[str, Any]] = []
    for level in chosen:
        candidate_id = str(level["id"])
        label = ("지지" if level["role"] == "support" else "저항") + (" · 매물대" if level.get("vpConfluence") else "")
        drawing = _drawing(
            symbol=symbol,
            interval=interval,
            layer="structure",
            suffix=_stable_suffix(candidate_id),
            drawing_type="horizontalLine",
            anchors=[{"price": round(float(level["price"]), 2)}],
            style_token=_level_style(float(level["score"])),
            label=label,
            generated_at=generated_at,
            created_by="system",
        )
        structure_drawings.append(drawing)
        structure_selected.append({
            "candidateId": candidate_id,
            "drawingIds": [drawing["id"]],
            "evidenceRefs": level.get("memberPivotIds") or [],
            "quality": {
                "score": level["score"],
                "touchEpisodes": level.get("touches", 0),
                "lastTouchAgeBars": level.get("lastTouchAgeBars"),
                "currentDistanceAtr": level.get("currentDistanceAtr"),
            },
        })

    events = sorted(
        (
            item for item in all_events
            if item.get("hardPass") and item.get("timestamp") in candle_times
        ),
        key=lambda item: (
            {"high": 0, "medium": 1}.get(item.get("currentImpact"), 2),
            int(item.get("ageBars") or 0),
            item["id"],
        ),
    )
    if events and len(structure_drawings) < 3:
        event = events[0]
        candidate_id = str(event["id"])
        drawing = _drawing(
            symbol=symbol,
            interval=interval,
            layer="structure",
            suffix=_stable_suffix(candidate_id),
            drawing_type="flagMarker",
            anchors=[{"timestamp": event["timestamp"], "price": round(float(event["price"]), 2)}],
            style_token="asset-flag",
            label=_event_label(event),
            generated_at=generated_at,
            created_by="system",
        )
        structure_drawings.append(drawing)
        structure_selected.append({
            "candidateId": candidate_id,
            "drawingIds": [drawing["id"]],
            "evidenceRefs": [candidate_id, *event.get("refIds", [])],
            "quality": {
                "score": 0.8 if event.get("currentImpact") == "high" else 0.65,
                "ageBars": event.get("ageBars"),
                "currentImpact": event.get("currentImpact"),
                "eventState": (event.get("detail") or {}).get("state"),
            },
        })

    structure_rejected = [item for item in (*all_levels, *all_events) if not item.get("hardPass")]
    structure_empty_reason = _empty_reason(
        [*all_levels, *all_events], features.get("qualityFlags") or [], structure_drawings,
    )
    structure = {
        "drawings": structure_drawings,
        "selected": structure_selected,
        "emptyReason": structure_empty_reason,
        "meta": {
            "candidateCount": len(all_levels) + len(all_events),
            "passedCount": len(levels) + len(events),
            "rejectedByReason": _reason_counts(structure_rejected),
            "qualityState": "ready" if structure_drawings else "quality_empty",
        },
    }

    all_trends = list(features.get("trends", []))
    trends = [item for item in all_trends if item.get("hardPass")]
    trend_drawings: list[dict[str, Any]] = []
    trend_selected: list[dict[str, Any]] = []
    if trends:
        trend = sorted(
            trends,
            key=lambda item: (
                -float(item.get("score") or 0),
                float(item["currentDistanceAtr"]) if item.get("currentDistanceAtr") is not None else 99.0,
                item["id"],
            ),
        )[0]
        pivots = {item["id"]: item for item in features.get("pivots", [])}
        anchors: list[dict[str, Any]] = []
        drawing_type = "trendLine"
        label = "검증된 추세"
        if trend["kind"] == "range":
            drawing_type = "rangeBox"
            label = "검증된 횡보 구간"
            anchors = [
                {"timestamp": trend["rangeFrom"], "price": float(trend["rangeHigh"])},
                {"timestamp": trend["rangeTo"], "price": float(trend["rangeLow"])},
            ]
        else:
            ids = trend.get("anchorPivotIds", [])[:3 if trend["kind"] == "channel" else 2]
            anchors = [
                {"timestamp": pivots[item]["timestamp"], "price": float(pivots[item]["price"])}
                for item in ids if item in pivots
            ]
            drawing_type = "trendParallelLines" if trend["kind"] == "channel" else "trendLine"
            label = "검증된 채널" if trend["kind"] == "channel" else ("상승 추세" if trend["kind"] == "up" else "하락 추세")

        required = 3 if drawing_type == "trendParallelLines" else 2
        if len(anchors) == required and all(anchor["timestamp"] in candle_times for anchor in anchors):
            drawing = _drawing(
                symbol=symbol,
                interval=interval,
                layer="trend",
                suffix=_stable_suffix(trend["id"]),
                drawing_type=drawing_type,
                anchors=anchors,
                style_token="asset-range" if drawing_type == "rangeBox" else "asset-trend",
                label=label,
                generated_at=generated_at,
                created_by="system",
                parallel_line_count=2 if drawing_type == "trendParallelLines" else None,
            )
            trend_drawings = [drawing]
            trend_selected = [{
                "candidateId": trend["id"],
                "drawingIds": [drawing["id"]],
                "evidenceRefs": trend.get("touchPivotIds") or trend.get("anchorPivotIds") or [],
                "quality": {
                    key: trend.get(key)
                    for key in (
                        "score", "touches", "currentDistanceAtr", "lastTouchAgeBars",
                        "spanBars", "medianResidualAtr", "violationCount",
                    )
                },
            }]

    all_patterns = list(features.get("patterns", []))
    patterns = [item for item in all_patterns if item.get("hardPass")]
    if patterns:
        pattern = sorted(patterns, key=lambda item: (-float(item.get("score") or 0), item["id"]))[0]
        pattern_drawings = _compile_pattern_drawings(
            symbol=symbol,
            interval=interval,
            pattern=pattern,
            candle_times=candle_times,
            generated_at=generated_at,
        )
        if len(pattern_drawings) == 2:
            trend_drawings.extend(pattern_drawings)
            trend_selected.append({
                "candidateId": pattern["id"],
                "drawingIds": [item["id"] for item in pattern_drawings],
                "evidenceRefs": pattern.get("evidenceRefs") or [],
                "quality": {
                    key: pattern.get(key)
                    for key in (
                        "score", "state", "breakoutDirection", "touches", "containment",
                        "convergenceRatio", "poleAtr", "retracementRatio", "spanBars",
                    )
                    if pattern.get(key) is not None
                },
                "patternKind": pattern.get("kind"),
                "patternState": pattern.get("state"),
            })

    # A pattern is one semantic structure but needs two engine drawings. Keep
    # the existing five-entity foreground contract by yielding lower-priority
    # S-layer drawings when the T layer grows.
    available_structure = max(0, 5 - len(trend_drawings))
    if len(structure_drawings) > available_structure:
        kept_ids = {item["id"] for item in structure_drawings[:available_structure]}
        structure_drawings = structure_drawings[:available_structure]
        structure_selected = [
            item for item in structure_selected
            if any(drawing_id in kept_ids for drawing_id in item.get("drawingIds", []))
        ]
        structure["drawings"] = structure_drawings
        structure["selected"] = structure_selected

    trend_candidates = [*all_trends, *all_patterns]
    trend_layer = {
        "drawings": trend_drawings,
        "selected": trend_selected,
        "emptyReason": _empty_reason(trend_candidates, features.get("qualityFlags") or [], trend_drawings),
        "meta": {
            "candidateCount": len(trend_candidates),
            "passedCount": len(trends) + len(patterns),
            "rejectedByReason": _reason_counts([item for item in trend_candidates if not item.get("hardPass")]),
            "qualityState": "ready" if trend_drawings else "quality_empty",
        },
    }
    return {"structure": structure, "trend": trend_layer}


def _compile_pattern_drawings(*, symbol, interval, pattern, candle_times, generated_at):
    geometry = pattern.get("geometry") or {}
    kind = str(pattern.get("kind") or "")
    state = str(pattern.get("state") or "forming")
    style_token = _pattern_style(kind)
    label = _pattern_label(kind, state)
    candidate_id = str(pattern["id"])
    drawings = []
    if kind.endswith("_triangle"):
        for boundary_name in ("upper", "lower"):
            boundary = geometry.get(boundary_name) or {}
            anchors = [boundary.get("start"), boundary.get("end")]
            if not _valid_pattern_anchors(anchors, candle_times):
                return []
            drawing = _drawing(
                symbol=symbol, interval=interval, layer="trend",
                suffix=_stable_suffix(f"{candidate_id}:{boundary_name}"),
                drawing_type="trendLine", anchors=anchors,
                style_token=style_token,
                label=label if boundary_name == "upper" else "패턴 하단 경계",
                generated_at=generated_at, created_by="system",
            )
            _apply_pattern_state_style(drawing, state)
            drawings.append(drawing)
        return drawings
    if kind not in {"bullish_flag", "bearish_flag"}:
        return []
    pole = geometry.get("pole") or {}
    pole_anchors = [pole.get("start"), pole.get("end")]
    upper = geometry.get("upper") or {}
    lower = geometry.get("lower") or {}
    channel_anchors = [upper.get("start"), upper.get("end"), lower.get("start")]
    if not _valid_pattern_anchors(pole_anchors, candle_times) or not _valid_pattern_anchors(channel_anchors, candle_times):
        return []
    pole_drawing = _drawing(
        symbol=symbol, interval=interval, layer="trend",
        suffix=_stable_suffix(f"{candidate_id}:pole"),
        drawing_type="trendLine", anchors=pole_anchors,
        style_token=style_token, label=label,
        generated_at=generated_at, created_by="system",
    )
    channel_drawing = _drawing(
        symbol=symbol, interval=interval, layer="trend",
        suffix=_stable_suffix(f"{candidate_id}:channel"),
        drawing_type="trendParallelLines", anchors=channel_anchors,
        style_token=style_token, label="깃발 채널",
        generated_at=generated_at, created_by="system", parallel_line_count=2,
    )
    _apply_pattern_state_style(pole_drawing, state)
    _apply_pattern_state_style(channel_drawing, state)
    return [pole_drawing, channel_drawing]


def _valid_pattern_anchors(anchors, candle_times):
    return all(
        isinstance(anchor, dict)
        and anchor.get("timestamp") in candle_times
        and isinstance(anchor.get("price"), (int, float))
        for anchor in anchors
    )


def _apply_pattern_state_style(drawing, state):
    if state == "forming":
        drawing["style"]["lineDash"] = [6, 4]
        drawing["style"]["opacity"] = 0.78


def _pattern_style(kind):
    if kind in {"ascending_triangle", "bullish_flag"}:
        return "asset-pattern-bull"
    if kind in {"descending_triangle", "bearish_flag"}:
        return "asset-pattern-bear"
    return "asset-pattern-neutral"


def _pattern_label(kind, state):
    names = {
        "ascending_triangle": "상승 삼각형",
        "descending_triangle": "하락 삼각형",
        "symmetrical_triangle": "대칭 삼각형",
        "bullish_flag": "상승 깃발",
        "bearish_flag": "하락 깃발",
    }
    state_label = "돌파 확인" if state == "confirmed" else "형성 중"
    return f"{names.get(kind, '차트 패턴')} · {state_label}"


def _drawing(
    *,
    symbol: str,
    interval: str,
    layer: str,
    suffix: str,
    drawing_type: str,
    anchors: list[dict[str, Any]],
    style_token: str,
    label: str,
    generated_at: str,
    created_by: str,
    parallel_line_count: int | None = None,
) -> dict[str, Any]:
    drawing = {
        "id": f"ca-{symbol}-{interval}-{layer}-{suffix}",
        "type": drawing_type,
        "anchors": anchors,
        "sourceInterval": interval,
        "style": dict(STYLE_TOKENS[style_token]),
        "label": label,
        "locked": False,
        "visible": True,
        "createdBy": created_by,
        "sourceProposalId": f"chart-asset:{symbol}:{interval}:{layer}",
        "createdAt": generated_at,
        "updatedAt": generated_at,
    }
    if parallel_line_count is not None:
        drawing["parallelLineCount"] = parallel_line_count
    return drawing


def _empty_layer(reason: str) -> dict[str, Any]:
    return {
        "drawings": [],
        "selected": [],
        "emptyReason": reason,
        "meta": {"candidateCount": 0, "passedCount": 0, "rejectedByReason": {}, "qualityState": "data_degraded" if reason == "data_quality_blocked" else "quality_empty"},
    }


def _stable_suffix(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:10]


def _level_rank(item: dict[str, Any]) -> tuple[int, float, int, str]:
    distance = float(item["currentDistanceAtr"]) if item.get("currentDistanceAtr") is not None else 99.0
    band = 0 if distance <= 1.5 else 1
    age = int(item["lastTouchAgeBars"]) if item.get("lastTouchAgeBars") is not None else 999
    return band, -float(item.get("score") or 0), age, str(item["id"])


def _reason_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        for reason in item.get("rejectReasons") or ["hard_gate"]:
            result[reason] = result.get(reason, 0) + 1
    return result


def _empty_reason(candidates, quality_flags, drawings):
    if drawings:
        return None
    if "data_quality_blocked" in quality_flags:
        return "data_quality_blocked"
    if not candidates:
        return "no_structural_evidence"
    rejected = [item for item in candidates if not item.get("hardPass")]
    if rejected and all("active_invalidation" in (item.get("rejectReasons") or []) for item in rejected):
        return "active_invalidation"
    if any(item.get("evidencePass") for item in rejected):
        return "not_currently_actionable"
    return "no_structural_evidence"


def _level_style(score: float) -> str:
    if score >= 0.75:
        return "asset-sr-strong"
    if score >= 0.55:
        return "asset-sr-medium"
    return "asset-sr-weak"


def _event_label(event: dict[str, Any]) -> str:
    kind = event.get("kind")
    detail = event.get("detail") or {}
    if kind == "breakout":
        if detail.get("state") == "failed":
            return "지지 이탈 실패" if detail.get("direction") == "down" else "저항 돌파 실패"
        return "지지 이탈" if detail.get("direction") == "down" else "저항 돌파"
    if kind == "retest":
        return "저항 리테스트 확인" if detail.get("direction") == "down" else "지지 리테스트 확인"
    if kind == "52wHigh":
        return "52주 신고가"
    if kind == "52wLow":
        return "52주 신저가"
    if kind == "volumeSpike":
        return "거래량 급증"
    return str(kind or "이벤트")
