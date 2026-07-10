from __future__ import annotations

from typing import Any

from .compilers import STYLE_TOKENS
from .prompts import ALLOWED_INDICATORS, ALLOWED_ROLES, ALLOWED_STYLE_TOKENS, ALLOWED_TOOLS


ANCHOR_COUNTS = {
    "horizontalLine": 1,
    "trendLine": 2,
    "trendParallelLines": 3,
    "horizontalParallelLines": 2,
    "verticalParallelLines": 2,
    "rangeBox": 2,
    "fibonacciRetracement": 2,
    "flagMarker": 1,
    "textLabel": 1,
}
TIMED_TOOLS = set(ANCHOR_COUNTS).difference({"horizontalLine", "horizontalParallelLines"})
FILLED_TOOLS = {
    "trendParallelLines", "horizontalParallelLines", "verticalParallelLines",
    "rangeBox", "fibonacciRetracement",
}
FOREGROUND_TOOLS = set(ANCHOR_COUNTS).difference(FILLED_TOOLS)


def compile_agent_layer(
    *,
    symbol: str,
    interval: str,
    intents: list[dict[str, Any]],
    features: dict[str, Any],
    rule_layers: dict[str, Any],
    generated_at: str,
    model: str | None,
) -> dict[str, Any]:
    inventory = _anchor_inventory(features)
    fib_pairs = {
        (str(item.get("fromPivotId")), str(item.get("toPivotId")))
        for item in features.get("fibCandidates", [])
    }
    atr = float(features.get("regime", {}).get("atr14") or 0)
    rule_prices = _rule_horizontal_prices(rule_layers)
    drawings: list[dict[str, Any]] = []
    accepted_intents: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    foreground_count = 0
    fill_count = 0

    for intent in intents:
        tool = str(intent.get("tool") or "")
        anchor_ids = [str(value) for value in intent.get("anchorIds", [])]
        reason = _intent_validation_reason(tool, anchor_ids, inventory, fib_pairs)
        anchors = [inventory[anchor_id] for anchor_id in anchor_ids if anchor_id in inventory]
        if reason is None and tool == "horizontalLine" and rule_prices:
            price = float(anchors[0]["price"])
            tolerance = 0.5 * max(atr, 0.0)
            if any(abs(price - rule_price) <= tolerance for rule_price in rule_prices):
                reason = "rule_duplicate"
        if reason is None and tool in FOREGROUND_TOOLS and foreground_count >= 5:
            reason = "foreground_budget"
        if reason is None and tool in FILLED_TOOLS and fill_count >= 2:
            reason = "fill_budget"
        if reason is not None:
            dropped.append({"intent": intent, "reason": reason})
            continue
        if tool in FOREGROUND_TOOLS:
            foreground_count += 1
        if tool in FILLED_TOOLS:
            fill_count += 1
        accepted_intents.append(intent)
        drawings.append(_drawing_from_intent(
            symbol=symbol,
            interval=interval,
            index=len(drawings) + 1,
            intent=intent,
            anchors=anchors,
            generated_at=generated_at,
        ))

    return {
        "drawings": drawings,
        "intents": accepted_intents,
        "rationale": " ".join(str(item.get("rationale") or "").strip() for item in accepted_intents).strip(),
        "degraded": False,
        "model": model,
        "droppedIntents": dropped,
        "meta": {"groundingFlags": []},
    }


def merge_indicator_suggestions(
    rule_suggestions: list[dict[str, Any]],
    llm_suggestions: list[dict[str, Any]],
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, suggestions in (("rule", rule_suggestions), ("llm", llm_suggestions)):
        for item in suggestions:
            layer = str(item.get("layer") or "")
            reason = str(item.get("reason") or "").strip()
            if layer not in ALLOWED_INDICATORS or layer in seen or not reason:
                continue
            seen.add(layer)
            merged.append({"layer": layer, "reason": reason, "source": source})
            if len(merged) == 2:
                return merged
    return merged


def _intent_validation_reason(
    tool: str,
    anchor_ids: list[str],
    inventory: dict[str, dict[str, Any]],
    fib_pairs: set[tuple[str, str]],
) -> str | None:
    if tool not in ALLOWED_TOOLS:
        return "unsupported_tool"
    if len(anchor_ids) != ANCHOR_COUNTS[tool]:
        return "invalid_anchor_count"
    if any(anchor_id not in inventory for anchor_id in anchor_ids):
        return "unknown_anchor"
    if tool in TIMED_TOOLS and any(not inventory[anchor_id].get("timestamp") for anchor_id in anchor_ids):
        return "anchor_shape"
    if tool == "fibonacciRetracement" and tuple(anchor_ids) not in fib_pairs:
        return "fib_not_candidate"
    return None


def _anchor_inventory(features: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for item in features.get("pivots", []):
        inventory[str(item["id"])] = {"timestamp": item["timestamp"], "price": float(item["price"])}
    for item in features.get("levels", []):
        inventory[str(item["id"])] = {"price": float(item["price"])}
    for item in features.get("events", []):
        inventory[str(item["id"])] = {"timestamp": item["timestamp"], "price": float(item["price"])}
    return inventory


def _rule_horizontal_prices(rule_layers: dict[str, Any]) -> list[float]:
    prices: list[float] = []
    for drawing in rule_layers.get("structure", {}).get("drawings", []):
        if drawing.get("type") != "horizontalLine":
            continue
        anchor = (drawing.get("anchors") or [{}])[0]
        if anchor.get("price") is not None:
            prices.append(float(anchor["price"]))
    return prices


def _drawing_from_intent(
    *,
    symbol: str,
    interval: str,
    index: int,
    intent: dict[str, Any],
    anchors: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    tool = str(intent["tool"])
    style_token = str(intent["styleToken"])
    style = dict(STYLE_TOKENS[style_token])
    if tool in FILLED_TOOLS and "fillColor" not in style:
        style.update({
            "fillColor": style.get("color", "#33adff"),
            "fillToken": style_token,
            "fillOpacity": 0.07,
        })
    drawing = {
        "id": f"ca-{symbol}-{interval}-agent-{index}",
        "type": tool,
        "anchors": anchors,
        "sourceInterval": interval,
        "style": style,
        "label": str(intent.get("label") or "").strip(),
        "locked": False,
        "visible": True,
        "createdBy": "llm",
        "sourceProposalId": f"chart-asset:{symbol}:{interval}:agent",
        "createdAt": generated_at,
        "updatedAt": generated_at,
    }
    if tool == "trendParallelLines":
        drawing["parallelLineCount"] = 2
    return drawing


def validate_output_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"intents", "indicatorSuggestions", "commentary"}:
        raise ValueError("invalid top-level chart asset LLM output")
    intents = value.get("intents")
    suggestions = value.get("indicatorSuggestions")
    commentary = value.get("commentary")
    if not isinstance(intents, list) or len(intents) > 5:
        raise ValueError("invalid intents")
    if not isinstance(suggestions, list) or len(suggestions) > 2:
        raise ValueError("invalid indicator suggestions")
    for intent in intents:
        required = {"tool", "anchorIds", "styleToken", "label", "role", "rationale"}
        if not isinstance(intent, dict) or set(intent) != required:
            raise ValueError("invalid intent object")
        if intent["tool"] not in ALLOWED_TOOLS or intent["styleToken"] not in ALLOWED_STYLE_TOKENS or intent["role"] not in ALLOWED_ROLES:
            raise ValueError("invalid intent enum")
        if not isinstance(intent["anchorIds"], list) or not 1 <= len(intent["anchorIds"]) <= 3 or not all(isinstance(item, str) and item for item in intent["anchorIds"]):
            raise ValueError("invalid intent anchors")
        if not all(isinstance(intent[key], str) and intent[key].strip() for key in ("label", "rationale")):
            raise ValueError("invalid intent text")
    for suggestion in suggestions:
        if not isinstance(suggestion, dict) or set(suggestion) != {"layer", "reason"}:
            raise ValueError("invalid indicator suggestion")
        if suggestion["layer"] not in ALLOWED_INDICATORS or not isinstance(suggestion["reason"], str) or not suggestion["reason"].strip():
            raise ValueError("invalid indicator suggestion value")
    if not isinstance(commentary, dict) or set(commentary) != {"text", "keyLevels", "invalidation", "confidence"}:
        raise ValueError("invalid commentary")
    if not isinstance(commentary["text"], str) or not commentary["text"].strip():
        raise ValueError("invalid commentary text")
    if not isinstance(commentary["keyLevels"], list) or not 1 <= len(commentary["keyLevels"]) <= 4 or not all(isinstance(item, str) and item.strip() for item in commentary["keyLevels"]):
        raise ValueError("invalid commentary key levels")
    if not isinstance(commentary["invalidation"], str) or not commentary["invalidation"].strip():
        raise ValueError("invalid commentary invalidation")
    confidence = commentary["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise ValueError("invalid commentary confidence")
    return value
