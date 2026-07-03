from __future__ import annotations

from typing import Any

from ..orchestration.routing import route_intent
from ..orchestration.ui_intent import (
    UIIntent,
    compact_layout_panels,
    has_explicit_ui_operation,
    infer_ui_intent_fallback,
    infer_action_size_and_position,
    normalize_text,
    panel_aliases,
    resolve_ui_target,
)
from .schema import UI_PANEL_TYPES, ContentTask, UiTask


DEFAULT_WORKSPACE_PANEL_TYPES = ["chart", "newsFeed", "aiSummary"]


def deterministic_content_tasks(intent: str) -> list[ContentTask]:
    route = route_intent(intent, router_mode="rules")
    if route.source == "fallback":
        return []
    return content_tasks_from_route(route)


def content_tasks_from_route(route: Any) -> list[ContentTask]:
    return [
        ContentTask(
            taskType=task_type,
            confidence=route.confidence,
            source=route.source,
            reason=route.reason,
            roles=content_task_roles(task_type, route.selectedRoles),
        )
        for task_type in content_task_types_for_route(route.intentType)
    ]


def deterministic_ui_tasks(intent: str, layout_context: dict[str, Any]) -> list[UiTask]:
    panels = compact_layout_panels(layout_context if isinstance(layout_context, dict) else {})
    multi_task = infer_multi_panel_ui_task(intent, panels)
    if multi_task is not None:
        return [multi_task]
    ui_intent = resolve_ui_target(infer_ui_intent_fallback(intent, panels), panels, intent)
    return ui_tasks_from_ui_intent(ui_intent)


def infer_multi_panel_ui_task(intent: str, panels: list[dict[str, Any]]) -> UiTask | None:
    text = normalize_text(intent)
    if not text or not has_explicit_ui_operation(intent):
        return None
    explicit_panel_types = explicit_panel_types_from_text(text)
    has_many_hint = any(term in text for term in ("여러", "여러개", "여러패널", "여러창", "대시보드", "동시에", "한번에", "같이", "나란히", "모두", "전체"))
    has_generic_many_panel_request = has_many_hint and any(term in text for term in ("패널", "창", "화면", "레이아웃", "dashboard", "panel", "window", "layout"))
    if len(explicit_panel_types) < 2 and not has_generic_many_panel_request:
        return None
    action, size_intent, position_intent = infer_action_size_and_position(text)
    if action == "unknown":
        action = "open" if any(term in text for term in ("띄워", "열어", "추가", "켜", "open", "add")) else "arrange"
    if action == "focus":
        action = "open" if any(term in text for term in ("띄워", "열어", "추가", "켜", "open", "add")) else "arrange"
    target_panel_types = explicit_panel_types if len(explicit_panel_types) >= 2 else list(DEFAULT_WORKSPACE_PANEL_TYPES)
    layout_preset = None if len(explicit_panel_types) >= 2 else "default_workspace"
    target_panel_ids = [
        panel["id"]
        for panel in panels
        if panel.get("type") in set(target_panel_types)
    ]
    return UiTask(
        action=action,
        targetPanelTypes=target_panel_types,
        targetPanelIds=target_panel_ids,
        layoutPreset=layout_preset,
        sizeIntent=size_intent,
        positionIntent=position_intent,
        confidence=0.84 if explicit_panel_types else 0.72,
        source="ui-fallback",
        reason="Fallback matched a multi-panel layout request.",
    )


def explicit_panel_types_from_text(text: str) -> list[str]:
    matches = []
    for panel_type in UI_PANEL_TYPES:
        aliases = panel_aliases(panel_type)
        if any(normalize_text(alias) in text for alias in aliases):
            matches.append(panel_type)
    return [panel_type for panel_type in UI_PANEL_TYPES if panel_type in set(matches)]


def ui_tasks_from_ui_intent(ui_intent: UIIntent | None) -> list[UiTask]:
    if ui_intent is None or not ui_intent.isUiIntent:
        return []
    return [
        UiTask(
            action=ui_intent.action,
            targetPanelType=ui_intent.targetPanelType,
            targetPanelId=ui_intent.targetPanelId,
            sizeIntent=ui_intent.sizeIntent,
            positionIntent=ui_intent.positionIntent,
            confidence=ui_intent.confidence,
            source=ui_intent.source,
            reason=ui_intent.reason,
        )
    ]


def content_task_type_for_route(intent_type: str) -> str:
    normalized = str(intent_type or "").strip().lower()
    if normalized == "market-move":
        return "market_move"
    if "news" in normalized:
        return "news"
    if "chart" in normalized:
        return "chart"
    if "macro" in normalized:
        return "macro"
    if "ontology" in normalized:
        return "ontology"
    return "general"


def content_task_types_for_route(intent_type: str) -> list[str]:
    parts = [part.strip() for part in str(intent_type or "").strip().lower().split("+") if part.strip()]
    task_types = []
    for part in parts or [intent_type]:
        task_type = content_task_type_for_route(str(part))
        if task_type not in task_types:
            task_types.append(task_type)
    return task_types or ["general"]


def content_task_roles(task_type: str, selected_roles: Any) -> list[str]:
    if task_type in {"market_move", "general"} and isinstance(selected_roles, list):
        return list(selected_roles)
    return []


def content_tasks_are_only_panel_references(tasks: list[ContentTask], ui_tasks: list[UiTask], has_confirmed_entity: bool) -> bool:
    if not tasks or not ui_tasks or has_confirmed_entity:
        return False
    return all(task.source in {"rule", "selection"} and task.taskType in {"news", "chart", "macro", "ontology"} for task in tasks)
