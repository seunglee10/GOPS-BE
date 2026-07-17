from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from .routing import parse_openai_text_json


# Keep in sync with UI_PANEL_TYPES in intent_understanding/schema.py.
PANEL_TYPES = (
    "chart",
    "chartPatternList",
    "compareChart",
    "companyCompare",
    "marketIndices",
    "indexCommentary",
    "companyProfile",
    "companyMulti",
    "companyValuation",
    "companyProfitability",
    "companyStability",
    "popularStocks",
    "newsFeed",
    "indicatorCompare",
    "orderTicket",
    "orderFlowProfile",
    "portfolioDashboard",
    "portfolioHoldings",
    "portfolioMulti",
    "portfolioInvestment",
    "portfolioPerformance",
    "portfolioInvested",
    "portfolioDividend",
    "portfolioDiversification",
    "stockRecommendations",
    "themeRadar",
    "aiSummary",
    "ontologyGraph",
)
UI_ACTIONS = (
    "focus",
    "resize",
    "move",
    "open",
    "close",
    "arrange",
    "keep",
    "load",
    "tidy",
    "undo",
    "reset",
    "swap",
    "replace",
    "pin",
    "unpin",
    "save",
    "unknown",
)
UI_SIZE_INTENTS = ("max", "large", "small", "min")
UI_POSITION_INTENTS = ("top", "bottom", "left", "right", "center")


@dataclass
class UIIntent:
    isUiIntent: bool
    intentKind: str
    targetPanelType: str | None
    targetPanelId: str | None
    action: str
    sizeIntent: str | None
    positionIntent: str | None
    confidence: float
    reason: str
    source: str = "fallback"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def route_ui_intent(intent: str, layout_context: dict[str, Any], router_mode: str = "hybrid", runtime_context: Any | None = None) -> UIIntent:
    text = str(intent or "").strip()
    panels = compact_layout_panels(layout_context)
    if not text:
        return non_ui_intent("Empty user intent.")
    parser_result = parse_ui_query_for_compat(text, layout_context)
    if parser_result.tasks:
        return ui_intent_from_task(parser_result.tasks[0])
    if not parser_result.needs_classifier and is_content_request_without_ui_operation(text):
        return non_ui_intent("Content request did not include an explicit UI operation.")

    llm_intent = route_ui_intent_with_openai(text, panels, router_mode, runtime_context=runtime_context)
    if llm_intent:
        return resolve_ui_target(llm_intent, panels, text)

    return non_ui_intent("No confident UI parser match.")


def route_ui_intent_with_openai(
    intent: str,
    panels: list[dict[str, Any]],
    router_mode: str,
    runtime_context: Any | None = None,
) -> UIIntent | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or os.getenv("AGENT_UI_ROUTER_PROVIDER") == "deterministic" or router_mode == "rules":
        return None
    if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
        if not runtime_context.acquire_llm("ui-router"):
            return None

    try:
        payload = {
            "model": os.getenv("AGENT_UI_ROUTER_MODEL", os.getenv("AGENT_ROUTER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2"))),
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Decide whether the user is asking to change the visible UI layout. "
                        "Classify misspellings and informal wording by meaning. "
                        "Return UI intent only for layout/panel/view changes, not market analysis, not order execution, "
                        "and not filling an order form. Use only the supplied panel ids and panel types. "
                        "Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "userIntent": intent,
                            "availablePanels": panels,
                            "panelTypes": PANEL_TYPES,
                            "actions": UI_ACTIONS,
                            "sizeIntents": UI_SIZE_INTENTS,
                            "positionIntents": UI_POSITION_INTENTS,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ui_intent_route",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "isUiIntent": {"type": "boolean"},
                            "intentKind": {"type": "string", "enum": ["layout", "non-ui", "ambiguous"]},
                            "targetPanelType": {"type": ["string", "null"], "enum": [*PANEL_TYPES, None]},
                            "targetPanelId": {"type": ["string", "null"]},
                            "action": {"type": "string", "enum": UI_ACTIONS},
                            "sizeIntent": {"type": ["string", "null"], "enum": [*UI_SIZE_INTENTS, None]},
                            "positionIntent": {"type": ["string", "null"], "enum": [*UI_POSITION_INTENTS, None]},
                            "confidence": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "isUiIntent",
                            "intentKind",
                            "targetPanelType",
                            "targetPanelId",
                            "action",
                            "sizeIntent",
                            "positionIntent",
                            "confidence",
                            "reason",
                        ],
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
        with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_UI_ROUTER_TIMEOUT_SECONDS", "8"))) as response:
            data = json.loads(response.read().decode("utf-8"))
        parsed = parse_openai_text_json(data)
        return normalize_ui_intent(parsed, "ui-llm")
    except Exception:
        return None


def normalize_ui_intent(parsed: dict[str, Any], source: str) -> UIIntent | None:
    if not isinstance(parsed, dict):
        return None
    action = parsed.get("action")
    size_intent = parsed.get("sizeIntent")
    position_intent = parsed.get("positionIntent")
    panel_type = parsed.get("targetPanelType")
    if action not in UI_ACTIONS:
        action = "unknown"
    if size_intent not in UI_SIZE_INTENTS:
        size_intent = None
    if position_intent not in UI_POSITION_INTENTS:
        position_intent = None
    if panel_type not in PANEL_TYPES:
        panel_type = None
    confidence = parsed.get("confidence")
    return UIIntent(
        isUiIntent=bool(parsed.get("isUiIntent")),
        intentKind=str(parsed.get("intentKind") or "ambiguous"),
        targetPanelType=panel_type,
        targetPanelId=str(parsed.get("targetPanelId")).strip() if parsed.get("targetPanelId") else None,
        action=action,
        sizeIntent=size_intent,
        positionIntent=position_intent,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
        reason=str(parsed.get("reason") or "UI intent router returned a structured result."),
        source=source,
    )


def resolve_ui_target(ui_intent: UIIntent, panels: list[dict[str, Any]], intent: str = "") -> UIIntent:
    panel_by_id = {panel["id"]: panel for panel in panels}
    target_id = ui_intent.targetPanelId if ui_intent.targetPanelId in panel_by_id else None
    target_type = ui_intent.targetPanelType
    if target_id and not target_type:
        target_type = panel_by_id[target_id]["type"]
    if target_type not in PANEL_TYPES:
        target_type = None
    if target_type and not target_id:
        target = next((panel for panel in panels if panel["type"] == target_type), None)
        target_id = target["id"] if target else None

    is_ui = (
        ui_intent.isUiIntent
        and ui_intent.intentKind == "layout"
        and ui_intent.confidence >= float(os.getenv("AGENT_UI_ROUTER_MIN_CONFIDENCE", "0.65"))
        and (target_type is not None or ui_intent.action == "open")
        and ui_intent.action != "unknown"
        and has_explicit_ui_operation(intent)
        and not is_content_request_without_ui_operation(intent)
    )
    return UIIntent(
        isUiIntent=is_ui,
        intentKind="layout" if is_ui else ui_intent.intentKind,
        targetPanelType=target_type,
        targetPanelId=target_id,
        action=ui_intent.action,
        sizeIntent=ui_intent.sizeIntent,
        positionIntent=ui_intent.positionIntent,
        confidence=ui_intent.confidence,
        reason=ui_intent.reason,
        source=ui_intent.source,
    )


def infer_ui_intent_fallback(intent: str, panels: list[dict[str, Any]]) -> UIIntent:
    parser_result = parse_ui_query_for_compat(intent, {"panels": panels})
    if not parser_result.tasks:
        return non_ui_intent("No confident fallback UI target/action match.")
    return ui_intent_from_task(parser_result.tasks[0])


def is_confident_fallback_ui_action(text: str, size_intent: str | None) -> bool:
    return bool(size_intent or has_explicit_ui_operation(text))


def is_content_request_without_ui_operation(intent: str) -> bool:
    text = normalize_text(intent)
    if not text:
        return False
    content_terms = (
        "뉴스",
        "기사",
        "보도",
        "헤드라인",
        "속보",
        "최신뉴스",
        "관련뉴스",
        "뉴스요약",
        "기사요약",
        "news",
        "headline",
        "headlines",
        "article",
        "articles",
        "latestnews",
        "relatednews",
    )
    has_content_term = any(term in text for term in content_terms)
    return has_content_term and not has_explicit_ui_operation(text)


def has_explicit_ui_operation(intent: str) -> bool:
    from ..intent_understanding.ui_parser import has_ui_operation_signal

    return has_ui_operation_signal(intent)


def non_ui_intent(reason: str) -> UIIntent:
    return UIIntent(
        isUiIntent=False,
        intentKind="non-ui",
        targetPanelType=None,
        targetPanelId=None,
        action="unknown",
        sizeIntent=None,
        positionIntent=None,
        confidence=0.0,
        reason=reason,
    )


def compact_layout_panels(layout_context: dict[str, Any]) -> list[dict[str, Any]]:
    from ..intent_understanding.ui_parser import compact_layout_panels_for_parser

    return compact_layout_panels_for_parser(layout_context if isinstance(layout_context, dict) else {})


def match_panel(text: str, panels: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any]]] = []
    for panel in panels:
        aliases = panel_aliases(panel["type"], panel.get("aliases"))
        aliases.append(str(panel.get("title") or ""))
        score = max((alias_match_score(text, alias) for alias in aliases), default=0)
        if score:
            scored.append((score, panel))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return scored[0][1]


def infer_action_size_and_position(text: str) -> tuple[str, str | None, str | None]:
    from ..intent_understanding.ui_parser import infer_action_size_and_position_from_query

    return infer_action_size_and_position_from_query(text)


def infer_position(text: str) -> str | None:
    return infer_action_size_and_position(text)[2]


def alias_match_score(text: str, alias: str) -> int:
    normalized_alias = normalize_text(alias)
    if not normalized_alias:
        return 0
    if normalized_alias in text:
        return len(normalized_alias) + 5
    compact_alias = normalized_alias.replace("패널", "").replace("창", "")
    if compact_alias and compact_alias in text:
        return len(compact_alias)
    return 0


def normalize_text(value: str) -> str:
    return "".join(str(value or "").lower().split())


def default_panel_title(panel_type: str) -> str:
    from ..intent_understanding.ui_parser import default_panel_title as parser_default_panel_title

    return parser_default_panel_title(panel_type)


def panel_aliases(panel_type: str, supplied: Any = None) -> list[str]:
    from ..intent_understanding.ui_parser import panel_aliases_for_type

    return panel_aliases_for_type(panel_type, supplied=supplied)


def parse_ui_query_for_compat(intent: str, layout_context: dict[str, Any]):
    from ..intent_understanding.ui_parser import parse_ui_query

    return parse_ui_query(intent, layout_context)


def ui_intent_from_task(task: Any) -> UIIntent:
    return UIIntent(
        isUiIntent=True,
        intentKind="layout",
        targetPanelType=getattr(task, "targetPanelType", None),
        targetPanelId=getattr(task, "targetPanelId", None),
        action=str(getattr(task, "action", "focus") or "focus"),
        sizeIntent=getattr(task, "sizeIntent", None),
        positionIntent=getattr(task, "positionIntent", None),
        confidence=float(getattr(task, "confidence", 0.7) or 0.7),
        reason=str(getattr(task, "reason", "UI task from parser.")),
        source=str(getattr(task, "source", "ui-parser")),
    )
