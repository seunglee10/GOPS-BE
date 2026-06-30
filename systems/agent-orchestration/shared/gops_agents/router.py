from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from .contracts import IntentRoute


ROLE_ORDER = ["chart", "news", "macro", "ontology"]
AGENT_ID_TO_ROLE = {
    "agent-01": "chart",
    "agent-02": "news",
    "agent-03": "macro",
    "agent-04": "ontology",
    "chart-agent": "chart",
    "news-agent": "news",
    "macro-agent": "macro",
    "ontology-agent": "ontology",
}
KEYWORD_ROUTES = [
    (("급등", "급락", "극락", "이상", "변동", "원인", "왜", "surge", "spike", "move", "why"), ROLE_ORDER, "market-move"),
    (("뉴스", "기사", "보도", "헤드라인", "news", "headline", "article"), ["news"], "news"),
    (("차트", "캔들", "가격", "추세", "chart", "candle", "price", "trend"), ["chart"], "chart"),
    (("거시", "금리", "cpi", "fomc", "macro", "rate", "inflation"), ["macro"], "macro"),
    (("관계", "온톨로지", "공급망", "경쟁사", "섹터", "ontology", "relationship", "supply"), ["ontology"], "ontology"),
]


def route_intent(intent: str, agent_ids: Any = None, router_mode: str = "hybrid") -> IntentRoute:
    text = str(intent or "").strip()
    lowered = text.lower()
    market_move_keywords, market_move_roles, market_move_type = KEYWORD_ROUTES[0]
    if any(keyword in lowered for keyword in market_move_keywords):
        return IntentRoute(
            source="rule",
            intentType=market_move_type,
            selectedRoles=list(market_move_roles),
            confidence=0.9,
            reason=f"Matched intent keyword for {market_move_type}.",
        )

    matched_roles = set()
    matched_types = []
    for keywords, roles, intent_type in KEYWORD_ROUTES[1:]:
        if any(keyword in lowered for keyword in keywords):
            matched_roles.update(roles)
            matched_types.append(intent_type)
    if matched_roles:
        ordered_roles = [role for role in ROLE_ORDER if role in matched_roles]
        intent_type = "+".join(matched_types)
        return IntentRoute(
            source="rule",
            intentType=intent_type,
            selectedRoles=ordered_roles,
            confidence=0.9,
            reason=f"Matched intent keyword for {intent_type}.",
        )

    selected = roles_from_agent_ids(agent_ids)
    if selected:
        intent_type = "+".join(selected)
        return IntentRoute(
            source="selection",
            intentType=intent_type or "selected-agents",
            selectedRoles=selected,
            confidence=0.75,
            reason="No strong intent keyword matched; using selected agent hints.",
        )

    if router_mode in {"strict-llm", "llm"} or os.getenv("AGENT_ROUTER_PROVIDER") == "openai" or os.getenv("OPENAI_API_KEY"):
        llm_route = route_with_openai(text)
        if llm_route:
            return llm_route

    return IntentRoute(
        source="fallback",
        intentType="general-analysis",
        selectedRoles=list(ROLE_ORDER),
        confidence=0.5,
        reason="No keyword or selected agent hint matched; using all visible analysis roles.",
    )


def roles_from_agent_ids(agent_ids: Any) -> list[str]:
    if not isinstance(agent_ids, list):
        return []
    roles = []
    seen = set()
    for item in agent_ids:
        role = AGENT_ID_TO_ROLE.get(item) if isinstance(item, str) else None
        if role and role not in seen:
            roles.append(role)
            seen.add(role)
    return roles


def route_with_openai(intent: str) -> IntentRoute | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
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
