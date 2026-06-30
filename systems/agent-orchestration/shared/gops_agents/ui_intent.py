from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from .router import parse_openai_text_json


PANEL_TYPES = ("chart", "newsFeed", "indicatorCompare", "orderTicket", "portfolioHoldings", "aiSummary", "ontologyGraph")
UI_ACTIONS = ("focus", "resize", "move", "open", "close", "unknown")
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


def route_ui_intent(intent: str, layout_context: dict[str, Any], router_mode: str = "hybrid") -> UIIntent:
    text = str(intent or "").strip()
    panels = compact_layout_panels(layout_context)
    if not text:
        return non_ui_intent("Empty user intent.")

    llm_intent = route_ui_intent_with_openai(text, panels, router_mode)
    if llm_intent:
        return resolve_ui_target(llm_intent, panels)

    return resolve_ui_target(infer_ui_intent_fallback(text, panels), panels)


def route_ui_intent_with_openai(intent: str, panels: list[dict[str, Any]], router_mode: str) -> UIIntent | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or os.getenv("AGENT_UI_ROUTER_PROVIDER") == "deterministic" or router_mode == "rules":
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


def resolve_ui_target(ui_intent: UIIntent, panels: list[dict[str, Any]]) -> UIIntent:
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
    text = normalize_text(intent)
    target = match_panel(text, panels)
    action, size_intent, position_intent = infer_action_size_and_position(text)
    if not target or action == "unknown" or not is_confident_fallback_ui_action(text, size_intent):
        return non_ui_intent("No confident fallback UI target/action match.")
    confidence = 0.82 if size_intent or action in {"focus", "open", "close"} else 0.68
    return UIIntent(
        isUiIntent=True,
        intentKind="layout",
        targetPanelType=target["type"],
        targetPanelId=target["id"],
        action=action,
        sizeIntent=size_intent,
        positionIntent=position_intent,
        confidence=confidence,
        reason="Fallback matched a visible panel and layout action after the LLM UI router was unavailable.",
        source="ui-fallback",
    )


def is_confident_fallback_ui_action(text: str, size_intent: str | None) -> bool:
    surface_terms = ("패널", "창", "화면", "레이아웃", "ui", "panel", "window", "layout")
    strong_layout_terms = (
        "앞", "메인", "중심", "위로", "아래", "밑", "상단", "하단", "왼쪽", "오른쪽", "좌측", "우측",
        "재배치", "바꿔", "변경", "크게", "키워", "확대", "작게", "줄여", "축소",
    )
    return bool(size_intent or any(term in text for term in surface_terms) or any(term in text for term in strong_layout_terms))


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
    panels = layout_context.get("panels") if isinstance(layout_context, dict) else None
    if not isinstance(panels, list):
        return []
    compacted = []
    for item in panels:
        if not isinstance(item, dict):
            continue
        panel_id = str(item.get("id") or "").strip()
        panel_type = str(item.get("type") or "").strip()
        if not panel_id or panel_type not in PANEL_TYPES:
            continue
        compacted.append({
            "id": panel_id,
            "type": panel_type,
            "title": str(item.get("title") or default_panel_title(panel_type)),
            "aliases": panel_aliases(panel_type, item.get("aliases")),
            "variant": str(item.get("variant") or ""),
            "layoutPinned": bool(item.get("layoutPinned")),
            "placement": item.get("placement") if isinstance(item.get("placement"), dict) else {},
        })
    return compacted


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
    position_intent = infer_position(text)
    if any(token in text for token in ("제일크", "젤크", "최대", "가득", "full", "max", "maximum")):
        return "resize", "max", position_intent
    if any(token in text for token in ("크게", "키워", "확대", "넓게", "larg", "big")):
        return "resize", "large", position_intent
    if any(token in text for token in ("작게", "줄여", "축소", "최소", "small", "min")):
        return "resize", "min", position_intent
    if position_intent:
        return "move", None, position_intent
    if any(token in text for token in ("앞", "메인", "중심", "위로", "보여", "가져", "바꿔", "변경", "재배치", "ui", "layout", "focus", "front")):
        return "focus", None, position_intent
    if any(token in text for token in ("열어", "추가", "띄워", "open", "add")):
        return "open", None, position_intent
    if any(token in text for token in ("닫아", "숨겨", "제거", "close", "hide", "remove")):
        return "close", None, position_intent
    return "unknown", None, position_intent


def infer_position(text: str) -> str | None:
    if any(token in text for token in ("아래", "밑", "하단", "bottom", "down")):
        return "bottom"
    if any(token in text for token in ("위", "상단", "top", "up")):
        return "top"
    if any(token in text for token in ("왼쪽", "좌측", "left")):
        return "left"
    if any(token in text for token in ("오른쪽", "우측", "right")):
        return "right"
    if any(token in text for token in ("가운데", "중앙", "중심", "center", "middle")):
        return "center"
    return None


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
    return {
        "chart": "차트",
        "newsFeed": "시장 뉴스",
        "indicatorCompare": "지표 비교",
        "orderTicket": "주문",
        "portfolioHoldings": "내 투자",
        "aiSummary": "AI 요약",
        "ontologyGraph": "온톨로지",
    }.get(panel_type, panel_type)


def panel_aliases(panel_type: str, supplied: Any = None) -> list[str]:
    aliases = {
        "chart": ["차트", "캔들", "가격 그래프", "chart", "graph"],
        "newsFeed": ["뉴스", "시장 뉴스", "기사", "헤드라인", "news", "headline"],
        "indicatorCompare": ["지표", "지표 비교", "인디케이터", "거시", "indicator", "macro"],
        "orderTicket": ["주문", "주문 입력", "주문창", "매수창", "매도창", "오더", "order", "ticket"],
        "portfolioHoldings": ["내 투자", "보유종목", "잔고", "계좌", "포트폴리오", "portfolio", "holdings", "balance"],
        "aiSummary": ["AI 요약", "요약", "AI 어시스턴트", "assistant", "summary"],
        "ontologyGraph": ["온톨로지", "관계 그래프", "기업 관계", "ontology", "relationship"],
    }.get(panel_type, [panel_type])
    if isinstance(supplied, list):
        aliases.extend(str(item) for item in supplied if isinstance(item, str))
    return aliases
