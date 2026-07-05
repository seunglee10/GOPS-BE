from __future__ import annotations

from typing import Any

from ..orchestration.ui_intent import UIIntent
from .classifier import ClassifierResult
from .rules import content_tasks_are_only_panel_references
from .schema import (
    ContentTask,
    QueryUnderstanding,
    UiTask,
    intent_type_for_tasks,
    normalize_query,
    selected_roles_for_tasks,
)


def merge_understanding(
    *,
    query: str,
    entity_resolution: Any,
    rule_content_tasks: list[ContentTask],
    rule_ui_tasks: list[UiTask],
    classifier_result: ClassifierResult | None,
    timings: dict[str, float] | None = None,
) -> QueryUnderstanding:
    classifier_content = list(classifier_result.contentTasks) if classifier_result else []
    classifier_ui = list(classifier_result.uiTasks) if classifier_result else []
    content_tasks = merge_content_tasks([*rule_content_tasks, *classifier_content])
    ui_tasks = merge_ui_tasks([*rule_ui_tasks, *classifier_ui])
    has_confirmed_entity = getattr(entity_resolution, "status", None) == "confirmed"
    if content_tasks_are_only_panel_references(content_tasks, ui_tasks, has_confirmed_entity):
        content_tasks = []
    selected_roles = selected_roles_for_tasks(content_tasks)
    route_mode = route_mode_for_tasks(content_tasks, ui_tasks, classifier_result)
    intent_type = "ui-layout" if route_mode == "ui_layout" else intent_type_for_tasks(content_tasks)
    confidence = confidence_for_understanding(content_tasks, ui_tasks, classifier_result)
    source = source_for_understanding(content_tasks, ui_tasks, classifier_result)
    warnings = list(classifier_result.warnings) if classifier_result else []
    entities = []
    if hasattr(entity_resolution, "to_dict"):
        entities.append(entity_resolution.to_dict())
    return QueryUnderstanding(
        originalQuery=str(query or ""),
        normalizedQuery=normalize_query(query),
        routeMode=route_mode,
        intentType=intent_type,
        selectedRoles=selected_roles,
        contentTasks=content_tasks,
        uiTasks=ui_tasks,
        entities=entities,
        confidence=confidence,
        source=source,
        needsClarification=bool(getattr(entity_resolution, "needs_clarification", False)),
        warnings=warnings,
        timings=timings or {},
    )


def merge_content_tasks(tasks: list[ContentTask]) -> list[ContentTask]:
    best: dict[str, ContentTask] = {}
    for task in tasks:
        current = best.get(task.taskType)
        if current is None or task.confidence > current.confidence:
            best[task.taskType] = task
    order = ["market_move", "news", "chart", "macro", "ontology", "financial_comparison", "financial", "general"]
    return [best[item] for item in order if item in best]


def merge_ui_tasks(tasks: list[UiTask]) -> list[UiTask]:
    best: dict[tuple[str, str, str, tuple[str, ...], tuple[str, ...], str], UiTask] = {}
    for task in tasks:
        key = (
            task.action,
            task.targetPanelType or "",
            task.targetPanelId or "",
            tuple(task.targetPanelTypes),
            tuple(task.targetPanelIds),
            task.layoutPreset or "",
        )
        current = best.get(key)
        if current is None or task.confidence > current.confidence:
            best[key] = task
    return sorted(best.values(), key=lambda item: item.confidence, reverse=True)[:3]


def route_mode_for_tasks(content_tasks: list[ContentTask], ui_tasks: list[UiTask], classifier_result: ClassifierResult | None) -> str:
    classifier_mode = classifier_result.routeMode if classifier_result else None
    if classifier_mode in {"analysis", "ui_layout", "hybrid", "clarify"}:
        if classifier_mode == "analysis" and not content_tasks and ui_tasks:
            return "ui_layout"
        if classifier_mode == "ui_layout" and content_tasks and ui_tasks:
            return "hybrid"
        return classifier_mode
    if content_tasks and ui_tasks:
        return "hybrid"
    if ui_tasks:
        return "ui_layout"
    return "analysis"


def confidence_for_understanding(content_tasks: list[ContentTask], ui_tasks: list[UiTask], classifier_result: ClassifierResult | None) -> float:
    values = [task.confidence for task in content_tasks]
    values.extend(task.confidence for task in ui_tasks)
    if classifier_result:
        values.append(classifier_result.confidence)
    return max(values) if values else 0.5


def source_for_understanding(content_tasks: list[ContentTask], ui_tasks: list[UiTask], classifier_result: ClassifierResult | None) -> str:
    sources = []
    sources.extend(task.source for task in content_tasks)
    sources.extend(task.source for task in ui_tasks)
    if classifier_result and (classifier_result.contentTasks or classifier_result.uiTasks):
        sources.append(classifier_result.source)
    unique = []
    for source in sources:
        if source and source not in unique:
            unique.append(source)
    if not unique:
        return "fallback"
    if len(unique) == 1:
        return unique[0]
    return "+".join(unique)


def primary_ui_intent_from_understanding(understanding: QueryUnderstanding) -> UIIntent | None:
    if not understanding.uiTasks:
        return None
    task = understanding.uiTasks[0]
    return UIIntent(
        isUiIntent=True,
        intentKind="layout",
        targetPanelType=task.targetPanelType,
        targetPanelId=task.targetPanelId,
        action=task.action,
        sizeIntent=task.sizeIntent,
        positionIntent=task.positionIntent,
        confidence=task.confidence,
        reason=task.reason,
        source=task.source,
    )
