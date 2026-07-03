from __future__ import annotations

from typing import Any

from ..orchestration.routing import route_intent
from .schema import ContentTask, UiTask
from .ui_parser import parse_ui_query


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
    return parse_ui_query(intent, layout_context).task_list()


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
