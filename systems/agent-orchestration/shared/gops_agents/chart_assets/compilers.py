from __future__ import annotations

import hashlib
from typing import Any


STYLE_TOKENS: dict[str, dict[str, Any]] = {
    "asset-sr-strong": {"color": "#ffffff", "colorToken": "asset-sr-strong", "lineWidth": 2, "opacity": 0.95},
    "asset-sr-medium": {"color": "#ffffff", "colorToken": "asset-sr-medium", "lineWidth": 1.5, "opacity": 0.75},
    "asset-sr-weak": {"color": "#ffffff", "colorToken": "asset-sr-weak", "lineWidth": 1, "opacity": 0.55},
    "asset-flag": {"color": "#ff7a3d", "colorToken": "asset-flag", "lineWidth": 1, "opacity": 0.95},
    "asset-trend": {"color": "#0099ff", "colorToken": "asset-trend", "lineWidth": 1.5, "opacity": 0.9, "extension": "ray"},
    "asset-range": {"color": "#999999", "colorToken": "asset-range", "lineWidth": 1, "fillColor": "#999999", "fillToken": "asset-range", "fillOpacity": 0.06, "opacity": 0.8},
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
    all_events = list(features.get("events", []))
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
    structure = {
        "drawings": structure_drawings,
        "selected": structure_selected,
        "emptyReason": None if structure_drawings else "no_candidate_passed_current_relevance",
        "meta": {
            "candidateCount": len(all_levels) + len(all_events),
            "passedCount": len(levels) + len(events),
            "rejectedByReason": _reason_counts(structure_rejected),
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
                float(item.get("currentDistanceAtr") or 99),
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

    trend_layer = {
        "drawings": trend_drawings,
        "selected": trend_selected,
        "emptyReason": None if trend_drawings else "no_candidate_passed_current_relevance",
        "meta": {
            "candidateCount": len(all_trends),
            "passedCount": len(trends),
            "rejectedByReason": _reason_counts([item for item in all_trends if not item.get("hardPass")]),
        },
    }
    return {"structure": structure, "trend": trend_layer}


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
        "meta": {"candidateCount": 0, "passedCount": 0, "rejectedByReason": {}},
    }


def _stable_suffix(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:10]


def _level_rank(item: dict[str, Any]) -> tuple[int, float, int, str]:
    distance = float(item.get("currentDistanceAtr") or 99)
    band = 0 if distance <= 1.5 else 1
    return band, -float(item.get("score") or 0), int(item.get("lastTouchAgeBars") or 999), str(item["id"])


def _reason_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        for reason in item.get("rejectReasons") or ["hard_gate"]:
            result[reason] = result.get(reason, 0) + 1
    return result


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
        return "지지 이탈" if detail.get("direction") == "down" else "저항 돌파"
    if kind == "retest":
        return "저항 리테스트 확인" if detail.get("direction") == "down" else "지지 리테스트 확인"
    if kind == "52wHigh":
        return "52주 신고가"
    if kind == "52wLow":
        return "52주 신저가"
    if kind == "gap":
        return f"갭 {'상승' if detail.get('direction') == 'up' else '하락'}{'(미채움)' if detail.get('unfilled') else ''}"
    if kind == "volumeSpike":
        return "거래량 급증"
    return str(kind or "이벤트")
