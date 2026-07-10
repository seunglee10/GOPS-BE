from __future__ import annotations

import re
from typing import Any

from alfaka.analytics import DISPLAY_BARS


PROMPT_VERSION = "prompt-v1"

ALLOWED_TOOLS = (
    "horizontalLine", "trendLine", "trendParallelLines", "horizontalParallelLines",
    "verticalParallelLines", "rangeBox", "fibonacciRetracement", "flagMarker", "textLabel",
)
ALLOWED_STYLE_TOKENS = ("insight-primary", "insight-zone", "insight-warning", "insight-note")
ALLOWED_ROLES = ("pattern", "zone", "scenario", "event", "note")
ALLOWED_INDICATORS = ("bollinger:20:2", "macd:12:26:9", "rsi:14", "stochastic:14:3:3", "ema:20")

SYSTEM_PROMPT = """당신은 차트 구조 해설가입니다. 제공된 계산 결과와 anchor id만 사용해 strict JSON으로 답하세요.
- 가격 방향을 예측하거나 매수·매도를 권유하지 말고, 구조·위험·무효화 조건을 설명하세요. 뉴스나 사건의 인과를 단정하지 마세요.
- ruleLayers의 수평선·추세선과 중복 작도하지 마세요. 레이어 I는 패턴 구간, 되돌림, 이벤트 창, 짧은 결론처럼 rule이 표현하지 못한 정보에 집중하세요.
- Fibonacci는 fibCandidates에 있는 피벗 쌍에만 허용됩니다. 모든 작도 좌표는 anchorIds로만 지정하고 수치를 직접 만들지 마세요.
- higherTimeframeContext가 있으면 상위 주기와 정합되게 설명하고, 충돌하면 그 사실을 명시하세요. 없으면 현재 주기만 설명하세요.
- 해설은 현재 국면, 핵심 가격 구간, 최근 변화·이벤트, 무효화 조건 순서의 존댓말 한글 3~6문장으로 쓰세요. 과장과 이모지는 금지합니다.
- 1D는 수주, 1W는 수개월, 1M은 수년 시야를 사용하세요. 모든 수치는 입력 JSON에 존재하는 값만 인용하세요.
- riskRewardBox는 허용되지 않습니다."""


def build_llm_input(
    *,
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
    features: dict[str, Any],
    rule_layers: dict[str, Any],
    higher_assets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    display = candles[-DISPLAY_BARS[interval]:]
    current_price = float(candles[-1]["close"])
    pivots = [{
        "id": item["id"], "t": item["timestamp"], "price": item["price"],
        "kind": item["kind"], "strength": item.get("strength", 0),
        "inDisplayWindow": bool(item.get("inDisplayWindow")),
    } for item in features.get("pivots", [])]
    levels = [{
        "id": item["id"], "price": item["price"], "score": item.get("score", 0),
        "kind": "support" if float(item["price"]) <= current_price else "resistance",
        "vpConfluence": bool(item.get("vpConfluence")),
    } for item in features.get("levels", [])]
    events = [{
        "id": item["id"], "t": item["timestamp"], "price": item["price"], "kind": item["kind"],
    } for item in features.get("events", [])]
    return {
        "symbol": symbol,
        "interval": interval,
        "asOf": candles[-1]["timestamp"],
        "displayWindow": {
            "from": display[0]["timestamp"], "to": display[-1]["timestamp"], "bars": len(display),
        },
        "recentBars": [[
            item["timestamp"], item["open"], item["high"], item["low"], item["close"], item.get("volume", 0),
        ] for item in candles[-20:]],
        "regime": features.get("regime") or {},
        "anchors": {
            "pivots": pivots,
            "levels": levels,
            "events": events,
            "fibCandidates": list(features.get("fibCandidates") or []),
        },
        "ruleLayers": {
            "structure": _structure_rule_labels(rule_layers, features, current_price),
            "trend": _drawing_labels(rule_layers.get("trend")),
        },
        "higherTimeframeContext": build_higher_timeframe_context({
            source: higher_assets[source]
            for source in {"1M": (), "1W": ("1M",), "1D": ("1M", "1W")}[interval]
            if source in higher_assets
        }),
    }


def build_higher_timeframe_context(higher_assets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    context: dict[str, Any] = {}
    for interval in ("1M", "1W"):
        asset = higher_assets.get(interval)
        if not asset:
            continue
        levels = sorted(
            asset.get("features", {}).get("levels", []),
            key=lambda item: (-float(item.get("score") or 0), str(item.get("id") or "")),
        )[:2]
        context[interval] = {
            "regime": str(asset.get("features", {}).get("regime", {}).get("trend") or "range"),
            "trend": _higher_trend_label(asset),
            "keyLevels": [
                f"레벨 {float(item['price']):.2f} (score {float(item.get('score') or 0):.2f})"
                for item in levels if item.get("price") is not None
            ],
            "commentaryGist": _first_sentence(str(asset.get("commentary", {}).get("text") or "")),
        }
    return context or None


def chart_asset_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intents": {
                "type": "array", "minItems": 0, "maxItems": 5,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "tool": {"type": "string", "enum": list(ALLOWED_TOOLS)},
                        "anchorIds": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 3},
                        "styleToken": {"type": "string", "enum": list(ALLOWED_STYLE_TOKENS)},
                        "label": {"type": "string", "minLength": 1},
                        "role": {"type": "string", "enum": list(ALLOWED_ROLES)},
                        "rationale": {"type": "string", "minLength": 1},
                    },
                    "required": ["tool", "anchorIds", "styleToken", "label", "role", "rationale"],
                },
            },
            "indicatorSuggestions": {
                "type": "array", "minItems": 0, "maxItems": 2,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "layer": {"type": "string", "enum": list(ALLOWED_INDICATORS)},
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "required": ["layer", "reason"],
                },
            },
            "commentary": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "keyLevels": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 4},
                    "invalidation": {"type": "string", "minLength": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["text", "keyLevels", "invalidation", "confidence"],
            },
        },
        "required": ["intents", "indicatorSuggestions", "commentary"],
    }


def _drawing_labels(layer: Any) -> list[str]:
    if not isinstance(layer, dict):
        return []
    return [str(item.get("label")) for item in layer.get("drawings", []) if item.get("label")]


def _structure_rule_labels(
    rule_layers: dict[str, Any],
    features: dict[str, Any],
    current_price: float,
) -> list[str]:
    structure = rule_layers.get("structure") or {}
    selected_ids = set(structure.get("meta", {}).get("levelIds") or [])
    selected_levels = [item for item in features.get("levels", []) if item.get("id") in selected_ids]
    labels = [
        (
            f"{'지지' if float(item['price']) <= current_price else '저항'} {float(item['price']):.2f} "
            f"(score {float(item.get('score') or 0):.2f}{', 매물대' if item.get('vpConfluence') else ''})"
        )
        for item in selected_levels
    ]
    selected_prices = [float(item["price"]) for item in selected_levels]
    for drawing in structure.get("drawings", []):
        label = str(drawing.get("label") or "").strip()
        anchor = (drawing.get("anchors") or [{}])[0]
        price = anchor.get("price")
        if not label:
            continue
        if drawing.get("type") == "horizontalLine" and price is not None and any(
            abs(float(price) - selected) < 0.005 for selected in selected_prices
        ):
            continue
        labels.append(label)
    return labels


def _higher_trend_label(asset: dict[str, Any]) -> str:
    trend_layer = asset.get("layers", {}).get("trend", {})
    labels = _drawing_labels(trend_layer)
    if labels:
        return labels[0]
    return str(trend_layer.get("meta", {}).get("kind") or "range")


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    match = re.search(r".+?(?:다\.|[.!?])(?:\s|$)", stripped)
    return match.group(0).strip() if match else stripped
