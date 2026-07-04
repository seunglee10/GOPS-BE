from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.request
from typing import Any

from ..contracts import IntentRoute


ROLE_ORDER = ["chart", "news", "macro", "ontology"]
KEYWORD_ROUTES = [
    (("급등", "급락", "극락", "이상", "변동", "원인", "왜", "surge", "spike", "move", "why"), ROLE_ORDER, "market-move"),
    (("시장", "시황", "market summary", "market overview"), ["chart", "macro"], "market-summary"),
    (("뉴스", "기사", "보도", "헤드라인", "news", "headline", "article"), ["news"], "news"),
    (("차트", "캔들", "가격", "추세", "chart", "candle", "price", "trend"), ["chart"], "chart"),
    (("거시", "금리", "cpi", "fomc", "macro", "rate", "inflation"), ["macro"], "macro"),
    (("관계", "온톨로지", "공급망", "경쟁사", "섹터", "ontology", "relationship", "supply"), ["ontology"], "ontology"),
]


@dataclass(frozen=True)
class RouteSignal:
    intent_type: str
    roles: tuple[str, ...]
    confidence: float
    source: str
    reason: str


def route_intent(intent: str, router_mode: str = "hybrid", runtime_context: Any | None = None) -> IntentRoute:
    text = str(intent or "").strip()
    rule_route = merge_route_signals(collect_route_signals(text))
    if rule_route is not None:
        return rule_route

    if router_mode in {"strict-llm", "llm"} or os.getenv("AGENT_ROUTER_PROVIDER") == "openai":
        llm_route = route_with_openai(text, runtime_context=runtime_context)
        if llm_route:
            return llm_route

    return IntentRoute(
        source="fallback",
        intentType="general-analysis",
        selectedRoles=list(ROLE_ORDER),
        confidence=0.5,
        reason="No keyword matched; using all visible analysis roles.",
    )


def collect_route_signals(intent: str) -> list[RouteSignal]:
    lowered = str(intent or "").lower()
    signals = []
    for keywords, roles, intent_type in KEYWORD_ROUTES:
        if any(keyword in lowered for keyword in keywords):
            signals.append(
                RouteSignal(
                    intent_type=intent_type,
                    roles=tuple(roles),
                    confidence=0.9,
                    source="rule",
                    reason=f"Matched intent keyword for {intent_type}.",
                )
            )
    return signals


def merge_route_signals(signals: list[RouteSignal]) -> IntentRoute | None:
    if not signals:
        return None
    intent_types = []
    role_set = set()
    confidence = 0.0
    sources = []
    for signal in signals:
        if signal.intent_type not in intent_types:
            intent_types.append(signal.intent_type)
        role_set.update(signal.roles)
        confidence = max(confidence, signal.confidence)
        if signal.source not in sources:
            sources.append(signal.source)
    intent_type = "+".join(intent_types)
    return IntentRoute(
        source="+".join(sources) if sources else "rule",
        intentType=intent_type,
        selectedRoles=[role for role in ROLE_ORDER if role in role_set],
        confidence=confidence or 0.5,
        reason=f"Merged route signals for {intent_type}.",
    )


def route_with_openai(intent: str, runtime_context: Any | None = None) -> IntentRoute | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
        if not runtime_context.acquire_llm("router"):
            return None
    try:
        payload = {
            "model": os.getenv("AGENT_ROUTER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
            "input": [
                {
                    "role": "system",
                    "content": "Route a stock analysis request to chart, news, macro, ontology roles. Return strict JSON only.",
                },
                {"role": "user", "content": intent},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agent_intent_route",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "intentType": {"type": "string"},
                            "selectedRoles": {
                                "type": "array",
                                "items": {"type": "string", "enum": ROLE_ORDER},
                            },
                            "confidence": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["intentType", "selectedRoles", "confidence", "reason"],
                    },
                }
            },
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_ROUTER_TIMEOUT_SECONDS", "8"))) as response:
            data = json.loads(response.read().decode("utf-8"))
        parsed = parse_openai_text_json(data)
        roles = [role for role in parsed.get("selectedRoles", []) if role in ROLE_ORDER]
        if not roles:
            return None
        return IntentRoute(
            source="strict-llm",
            intentType=str(parsed.get("intentType") or "general-analysis"),
            selectedRoles=roles,
            confidence=float(parsed.get("confidence") or 0.6),
            reason=str(parsed.get("reason") or "OpenAI strict router selected roles."),
        )
    except Exception:
        return None


def parse_openai_text_json(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("output_text"), str):
        return json.loads(data["output_text"])
    for output in data.get("output", []):
        for content in output.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                return json.loads(text)
    return {}
