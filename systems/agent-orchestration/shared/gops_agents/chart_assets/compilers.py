from __future__ import annotations

from typing import Any


STYLE_TOKENS: dict[str, dict[str, Any]] = {
    "asset-sr-strong": {"color": "#f5f5f5", "colorToken": "asset-sr-strong", "lineWidth": 2, "opacity": 0.95},
    "asset-sr-medium": {"color": "#f5f5f5", "colorToken": "asset-sr-medium", "lineWidth": 1.5, "opacity": 0.75},
    "asset-sr-weak": {"color": "#f5f5f5", "colorToken": "asset-sr-weak", "lineWidth": 1, "opacity": 0.55},
    "asset-sr-macro": {"color": "#f5f5f5", "colorToken": "asset-sr-macro", "lineWidth": 1.5, "lineDash": [6, 4], "opacity": 0.82},
    "asset-flag": {"color": "#ff7a3d", "colorToken": "asset-flag", "lineWidth": 1, "opacity": 0.95},
    "asset-trend": {"color": "#0099ff", "colorToken": "asset-trend", "lineWidth": 1.5, "opacity": 0.9, "extension": "ray"},
    "asset-range": {"color": "#999999", "colorToken": "asset-range", "lineWidth": 1, "fillColor": "#999999", "fillToken": "asset-range", "fillOpacity": 0.06, "opacity": 0.8},
    "insight-primary": {"color": "#33adff", "colorToken": "insight-primary", "lineWidth": 1.5, "opacity": 0.9},
    "insight-zone": {"color": "#33adff", "colorToken": "insight-zone", "lineWidth": 1, "fillColor": "#33adff", "fillToken": "insight-zone", "fillOpacity": 0.07, "opacity": 0.85},
    "insight-warning": {"color": "#ff5577", "colorToken": "insight-warning", "lineWidth": 1.5, "opacity": 0.9},
    "insight-note": {"color": "#f5f5f5", "colorToken": "insight-note", "lineWidth": 1, "opacity": 0.9},
}

EVENT_PRIORITY = {"breakout": 0, "retest": 1, "52wHigh": 2, "52wLow": 2, "gap": 3, "volumeSpike": 4}


def compile_rule_layers(
    *,
    symbol: str,
    interval: str,
    features: dict[str, Any],
    candles: list[dict[str, Any]],
    generated_at: str,
    higher_assets: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    structure = compile_structure_layer(
        symbol=symbol,
        interval=interval,
        features=features,
        candles=candles,
        generated_at=generated_at,
        higher_assets=higher_assets or {},
    )
    trend = compile_trend_layer(
        symbol=symbol,
        interval=interval,
        features=features,
        generated_at=generated_at,
    )
    return {"structure": structure, "trend": trend}


def compile_structure_layer(
    *,
    symbol: str,
    interval: str,
    features: dict[str, Any],
    candles: list[dict[str, Any]],
    generated_at: str,
    higher_assets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not candles:
        return {"drawings": [], "meta": {"levelIds": [], "flagEventIds": [], "macroLevels": [], "droppedIds": []}}
    current = float(candles[-1]["close"])
    display_low = min(float(item["low"]) for item in candles)
    display_high = max(float(item["high"]) for item in candles)
    price_min = display_low - (display_high - display_low) * 0.1
    price_max = display_high + (display_high - display_low) * 0.1
    atr = float(features.get("regime", {}).get("atr14") or max((display_high - display_low) / 20, 0.01))

    macro_levels = _select_macro_levels(interval, higher_assets, current, price_min, price_max)
    local = [
        level for level in features.get("levels", [])
        if float(level.get("score") or 0) >= 0.35 and price_min <= float(level["price"]) <= price_max
    ]
    local = [
        level for level in local
        if not any(abs(float(level["price"]) - float(macro["price"])) <= 0.5 * atr for macro in macro_levels)
    ]
    above = sorted((item for item in local if float(item["price"]) > current), key=_level_rank)
    below = sorted((item for item in local if float(item["price"]) <= current), key=_level_rank)
    selected = below[:3] + above[:2]
    if len(selected) < 5:
        selected_ids = {item["id"] for item in selected}
        remainder = [item for item in sorted(local, key=_level_rank) if item["id"] not in selected_ids]
        selected.extend(remainder[:5 - len(selected)])
    selected = sorted(selected[:5], key=lambda item: float(item["price"]))
    selected_ids = [item["id"] for item in selected]
    dropped = [item["id"] for item in local if item["id"] not in selected_ids]

    drawings: list[dict[str, Any]] = []
    for macro in macro_levels:
        label = f"{_interval_label(macro['sourceInterval'])} {'저항' if float(macro['price']) > current else '지지'} {float(macro['price']):.2f}"
        drawings.append(_drawing(
            symbol, interval, "structure", len(drawings) + 1, "horizontalLine",
            [{"price": round(float(macro["price"]), 2)}], "asset-sr-macro", label, generated_at, "system",
        ))
    for level in selected:
        price = float(level["price"])
        side = "저항" if price > current else "지지"
        suffix = " · 매물대" if level.get("vpConfluence") else ""
        drawings.append(_drawing(
            symbol, interval, "structure", len(drawings) + 1, "horizontalLine",
            [{"price": round(price, 2)}], _level_style(float(level["score"])), f"{side} {price:.2f}{suffix}", generated_at, "system",
        ))

    events = _select_flag_events(features.get("events", []), candles[0]["timestamp"])
    for event in events:
        drawings.append(_drawing(
            symbol, interval, "structure", len(drawings) + 1, "flagMarker",
            [{"timestamp": event["timestamp"], "price": round(float(event["price"]), 2)}],
            "asset-flag", _event_label(event), generated_at, "system",
        ))
    return {
        "drawings": drawings,
        "meta": {
            "levelIds": selected_ids,
            "flagEventIds": [item["id"] for item in events],
            "macroLevels": macro_levels,
            "droppedIds": dropped,
        },
    }


def compile_trend_layer(
    *, symbol: str, interval: str, features: dict[str, Any], generated_at: str,
) -> dict[str, Any]:
    pivots = {item["id"]: item for item in features.get("pivots", [])}
    drawings: list[dict[str, Any]] = []
    dropped: list[str] = []
    kind = "range"
    for trend in sorted(features.get("trends", []), key=lambda item: (-int(item.get("touches") or 0), item.get("id", ""))):
        trend_kind = trend.get("kind")
        if trend_kind == "channel" and not drawings:
            anchors = [_pivot_anchor(pivots.get(pivot_id)) for pivot_id in trend.get("anchorPivotIds", [])[:3]]
            if all(anchors):
                drawings.append(_drawing(symbol, interval, "trend", 1, "trendParallelLines", anchors, "asset-trend", "상승 채널" if float(trend.get("slopePerBar") or 0) >= 0 else "하락 채널", generated_at, "system", parallel_line_count=2))
                kind = "channel"
            continue
        if trend_kind in {"up", "down"} and len(drawings) < 2:
            anchors = [_pivot_anchor(pivots.get(pivot_id)) for pivot_id in trend.get("anchorPivotIds", [])[:2]]
            if all(anchors):
                label = f"{'상승' if trend_kind == 'up' else '하락'} 추세선 (접점 {int(trend.get('touches') or 0)})"
                drawings.append(_drawing(symbol, interval, "trend", len(drawings) + 1, "trendLine", anchors, "asset-trend", label, generated_at, "system"))
                kind = "trendline"
            continue
        if trend_kind == "range" and not drawings:
            required = ("rangeFrom", "rangeTo", "rangeHigh", "rangeLow")
            if all(trend.get(key) is not None for key in required):
                anchors = [
                    {"timestamp": trend["rangeFrom"], "price": float(trend["rangeHigh"])},
                    {"timestamp": trend["rangeTo"], "price": float(trend["rangeLow"])},
                ]
                drawings.append(_drawing(symbol, interval, "trend", 1, "rangeBox", anchors, "asset-range", "횡보 구간", generated_at, "system"))
                kind = "range"
            continue
        dropped.append(str(trend.get("id") or "unknown"))
    return {"drawings": drawings, "meta": {"kind": kind, "droppedIds": dropped}}


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


def fallback_commentary(symbol: str, interval: str, features: dict[str, Any], current_price: float) -> dict[str, Any]:
    trend = str(features.get("regime", {}).get("trend") or "range")
    trend_text = {"up": "상승 추세", "down": "하락 추세", "range": "횡보"}.get(trend, "혼조")
    levels = sorted(features.get("levels", []), key=lambda item: -float(item.get("score") or 0))
    support = next((float(item["price"]) for item in levels if float(item["price"]) <= current_price), None)
    resistance = next((float(item["price"]) for item in levels if float(item["price"]) > current_price), None)
    support_text = f"{support:.2f}" if support is not None else "확인되지 않음"
    resistance_text = f"{resistance:.2f}" if resistance is not None else "확인되지 않음"
    latest_event = max(features.get("events", []), key=lambda item: item["timestamp"], default=None)
    event_text = f"최근 {_event_label(latest_event)} 이벤트가 관찰됐습니다." if latest_event else "뚜렷한 최근 구조 이벤트는 확인되지 않았습니다."
    invalidation = f"{support:.2f} 종가 이탈 시 현재 구조 해석은 무효입니다." if support is not None else f"{resistance:.2f} 종가 돌파 시 현재 구조 해석을 다시 확인해야 합니다." if resistance is not None else "새 구조 레벨 형성 시 현재 해석을 다시 확인해야 합니다."
    return {
        "text": f"{symbol} {interval} 기준 {trend_text} 국면입니다. 주요 지지는 {support_text}, 저항은 {resistance_text}입니다. {event_text} {invalidation}",
        "keyLevels": [value for value in (f"지지 {support_text}" if support is not None else None, f"저항 {resistance_text}" if resistance is not None else None) if value],
        "invalidation": invalidation,
        "confidence": 0.3,
        "enrichment": None,
    }


def _drawing(
    symbol: str, interval: str, layer: str, index: int, drawing_type: str,
    anchors: list[dict[str, Any]], style_token: str, label: str, generated_at: str,
    created_by: str, parallel_line_count: int | None = None,
) -> dict[str, Any]:
    drawing = {
        "id": f"ca-{symbol}-{interval}-{layer}-{index}",
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


def _select_macro_levels(interval: str, higher_assets: dict[str, dict[str, Any]], current: float, price_min: float, price_max: float) -> list[dict[str, Any]]:
    sources = ("1M",) if interval == "1W" else ("1M", "1W") if interval == "1D" else ()
    candidates: list[dict[str, Any]] = []
    for source_interval in sources:
        asset = higher_assets.get(source_interval) or {}
        for level in asset.get("features", {}).get("levels", []):
            price = float(level["price"])
            if float(level.get("score") or 0) >= 0.7 and price_min <= price <= price_max:
                candidates.append({"price": round(price, 2), "sourceInterval": source_interval, "score": float(level["score"]), "label": f"{_interval_label(source_interval)} {'저항' if price > current else '지지'}"})
    candidates.sort(key=lambda item: (-item["score"], abs(item["price"] - current), item["sourceInterval"]))
    return candidates[:2]


def _select_flag_events(events: list[dict[str, Any]], display_from: str) -> list[dict[str, Any]]:
    ordered = sorted(
        (item for item in events if item.get("timestamp", "") >= display_from and item.get("kind") in EVENT_PRIORITY),
        key=lambda item: (EVENT_PRIORITY[item["kind"]], -_timestamp_rank(item["timestamp"])),
    )
    selected: list[dict[str, Any]] = []
    used_levels: set[str] = set()
    for event in ordered:
        refs = set(event.get("refIds") or [])
        if refs and refs.intersection(used_levels):
            continue
        selected.append(event)
        used_levels.update(refs)
        if len(selected) == 3:
            break
    return selected


def _event_label(event: dict[str, Any] | None) -> str:
    if not event:
        return "이벤트"
    kind = event.get("kind")
    detail = event.get("detail") or {}
    if kind == "breakout":
        return f"저항 돌파 · 거래량 {float(detail.get('volumeZ') or 0):.1f}×"
    if kind == "retest": return "지지 리테스트 확인"
    if kind == "52wHigh": return "52주 신고가"
    if kind == "52wLow": return "52주 신저가"
    if kind == "gap": return f"갭 {'상승' if detail.get('direction') == 'up' else '하락'}{'(미채움)' if detail.get('unfilled') else ''}"
    if kind == "volumeSpike": return f"거래량 급증 {float(detail.get('volumeZ') or 0):.1f}×"
    return str(kind or "이벤트")


def _level_rank(item: dict[str, Any]) -> tuple[float, float]:
    return -float(item.get("score") or 0), float(item["price"])


def _level_style(score: float) -> str:
    return "asset-sr-strong" if score >= 0.75 else "asset-sr-medium" if score >= 0.55 else "asset-sr-weak"


def _pivot_anchor(pivot: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"timestamp": pivot["timestamp"], "price": float(pivot["price"])} if pivot else None


def _interval_label(interval: str) -> str:
    return {"1M": "월봉", "1W": "주봉", "1D": "일봉"}.get(interval, interval)


def _timestamp_rank(timestamp: str) -> int:
    return int("".join(character for character in timestamp if character.isdigit())[:14] or 0)
