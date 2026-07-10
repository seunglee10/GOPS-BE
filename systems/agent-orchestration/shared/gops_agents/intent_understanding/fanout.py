from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
import os
import time
from typing import Any

from ..query_understanding import EntityResolution, extract_news_topic_from_intent, resolve_entity
from .classifier import ClassifierResult, build_intent_classifier_from_env
from .merger import merge_understanding
from .rules import content_tasks_are_only_panel_references, deterministic_content_tasks, deterministic_ui_tasks
from .schema import QueryUnderstanding

UI_ONLY_EARLY_RETURN_CONFIDENCE = 0.9
ANALYSIS_INTENT_TERMS = (
    "왜",
    "원인",
    "급등",
    "급락",
    "상승",
    "하락",
    "분석",
    "영향",
    "비교",
    "실적",
    "전망",
    "리스크",
    "호재",
    "악재",
    "earnings",
    "guidance",
    "impact",
    "analysis",
    "analyze",
    "compare",
    "why",
    "risk",
)
CONTENT_TASK_TERMS = (
    "뉴스",
    "거시",
    "관계",
    "재무",
    "실적",
    "news",
    "macro",
    "relationship",
    "financial",
    "earnings",
)


def build_query_understanding(
    query: str,
    *,
    agent_ids: Any = None,
    layout_context: dict[str, Any] | None = None,
    chart_context: Any = None,
    request_symbol: Any = None,
    runtime_context: Any | None = None,
    layout_command_preflight: bool = False,
    timing: dict[str, Any] | None = None,
) -> tuple[QueryUnderstanding, Any]:
    started_at = time.perf_counter()
    layout = layout_context if isinstance(layout_context, dict) else {}
    timeout_seconds = max(0.05, float(os.getenv("AGENT_QUERY_UNDERSTANDING_TIMEOUT_MS", "700")) / 1000)
    executor = ThreadPoolExecutor(max_workers=3)
    futures_by_name = {
        "entity": executor.submit(timed_call, resolve_entity, query, chart_context=chart_context),
        "content_rules": executor.submit(timed_call, deterministic_content_tasks, query),
        "ui_rules": executor.submit(timed_call, deterministic_ui_tasks, query, layout),
    }
    names_by_future = {future: name for name, future in futures_by_name.items()}
    results: dict[str, Any] = {}
    branch_timings: dict[str, float] = {}
    warnings: list[str] = []
    finished_branches: set[str] = set()
    early_return_reason: str | None = None
    try:
        try:
            for future in as_completed(names_by_future, timeout=timeout_seconds):
                name = names_by_future[future]
                collect_branch_result(
                    name,
                    future,
                    results=results,
                    branch_timings=branch_timings,
                    warnings=warnings,
                    finished_branches=finished_branches,
                )
                if should_return_ui_only_early(query=query, results=results):
                    early_return_reason = "ui_only"
                    cancel_unfinished(futures_by_name, finished_branches)
                    break
        except FutureTimeoutError:
            pass
        if early_return_reason is None:
            collect_or_timeout_unfinished(
                futures_by_name,
                results=results,
                branch_timings=branch_timings,
                warnings=warnings,
                finished_branches=finished_branches,
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    entity_resolution = results.get("entity")
    if entity_resolution is None:
        entity_resolution = EntityResolution(
            status="not_found",
            needs_clarification=False,
            reason=(
                "entity resolver skipped after high-confidence UI-only intent"
                if early_return_reason == "ui_only"
                else "entity resolver unavailable in parallel query understanding"
            ),
        )
    rule_content_tasks = list(results.get("content_rules") or [])
    raw_ui_tasks = results.get("ui_rules")
    if raw_ui_tasks is None:
        raw_ui_tasks = []
    rule_ui_tasks = list(raw_ui_tasks)
    ui_parser_needs_classifier = bool(getattr(raw_ui_tasks, "needs_classifier", False))
    for warning in getattr(raw_ui_tasks, "warnings", []):
        if warning not in warnings:
            warnings.append(str(warning))
    classifier_result = None
    classifier_required = False
    if early_return_reason is None:
        classifier_required = should_call_classifier_fallback(
            query=query,
            entity_resolution=entity_resolution,
            rule_content_tasks=rule_content_tasks,
            rule_ui_tasks=rule_ui_tasks,
            warnings=warnings,
            ui_parser_needs_classifier=ui_parser_needs_classifier,
            has_subject_fallback=has_subject_fallback(
                request_symbol=request_symbol,
                chart_context=chart_context,
            ),
        )
    if classifier_required:
        if acquire_llm(runtime_context, "intent-classifier"):
            try:
                classifier_result, classifier_ms = timed_call(
                    classify_with_provider,
                    query,
                    layout,
                    entity_resolution.to_dict() if hasattr(entity_resolution, "to_dict") else None,
                    True,
                )
                branch_timings["classifier"] = classifier_ms
            except Exception as exc:
                warnings.append(f"classifier_failed:{exc.__class__.__name__}")
        else:
            warnings.append("classifier_budget_blocked")
        if classifier_result is None:
            classifier_result = ClassifierResult(
                routeMode="clarify",
                confidence=0.0,
                source="intent-classifier-required",
                warnings=["intent_classifier_required_unavailable"],
            )
    if classifier_result is not None and warnings:
        classifier_result.warnings.extend(warnings)
    elif warnings:
        classifier_result = ClassifierResult(warnings=warnings, source="fallback")

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if isinstance(timing, dict):
        timing["queryUnderstandingMs"] = elapsed_ms
        if early_return_reason is not None:
            timing["queryUnderstandingEarlyReturn"] = early_return_reason
        if "entity" in branch_timings:
            timing["entityResolveMs"] = branch_timings["entity"]
        if classifier_required:
            timing["intentClassifierRequired"] = True
        if "classifier" in branch_timings:
            timing["intentClassifierMs"] = branch_timings["classifier"]
    understanding = merge_understanding(
        query=query,
        entity_resolution=entity_resolution,
        rule_content_tasks=rule_content_tasks,
        rule_ui_tasks=rule_ui_tasks,
        classifier_result=classifier_result,
        layout_context=layout,
        allow_content_display_ui=layout_command_preflight,
        timings={"totalMs": round(elapsed_ms, 3)},
    )
    return understanding, entity_resolution


def timed_call(func, *args, **kwargs) -> tuple[Any, float]:
    started_at = time.perf_counter()
    value = func(*args, **kwargs)
    return value, (time.perf_counter() - started_at) * 1000


def collect_branch_result(
    name: str,
    future: Any,
    *,
    results: dict[str, Any],
    branch_timings: dict[str, float],
    warnings: list[str],
    finished_branches: set[str],
) -> None:
    if name in finished_branches:
        return
    finished_branches.add(name)
    try:
        value, elapsed_ms = future.result()
        results[name] = value
        branch_timings[name] = elapsed_ms
    except Exception as exc:
        warnings.append(f"{name}_failed:{exc.__class__.__name__}")


def collect_or_timeout_unfinished(
    futures_by_name: dict[str, Any],
    *,
    results: dict[str, Any],
    branch_timings: dict[str, float],
    warnings: list[str],
    finished_branches: set[str],
) -> None:
    for name, future in futures_by_name.items():
        if name in finished_branches:
            continue
        if future.done():
            collect_branch_result(
                name,
                future,
                results=results,
                branch_timings=branch_timings,
                warnings=warnings,
                finished_branches=finished_branches,
            )
            continue
        future.cancel()
        finished_branches.add(name)
        warnings.append(f"{name}_timeout")


def cancel_unfinished(futures_by_name: dict[str, Any], finished_branches: set[str]) -> None:
    for name, future in futures_by_name.items():
        if name not in finished_branches and not future.done():
            future.cancel()


def should_return_ui_only_early(*, query: str, results: dict[str, Any]) -> bool:
    if bool_env("AGENT_INTENT_CLASSIFIER_ALWAYS", False):
        return False
    raw_ui_tasks = results.get("ui_rules")
    if raw_ui_tasks is None or bool(getattr(raw_ui_tasks, "needs_classifier", False)):
        return False
    ui_tasks = list(raw_ui_tasks)
    if not ui_tasks:
        return False
    max_confidence = max(
        (float(getattr(task, "confidence", 0.0) or 0.0) for task in ui_tasks),
        default=0.0,
    )
    if max_confidence < UI_ONLY_EARLY_RETURN_CONFIDENCE:
        return False
    if not any(getattr(task, "source", None) == "ui-parser" for task in ui_tasks):
        if not any(getattr(task, "source", None) == "ui-preset-parser" for task in ui_tasks):
            return False
    if has_analysis_intent_signal(query) and not has_preset_load_task(ui_tasks):
        return False
    if has_content_task_signal(query) and not has_preset_load_task(ui_tasks):
        return False
    if "content_rules" in results:
        content_tasks = list(results.get("content_rules") or [])
        if content_tasks and not content_tasks_are_only_panel_references(
            content_tasks,
            ui_tasks,
            has_confirmed_entity=False,
        ):
            return False
    return True


def has_analysis_intent_signal(query: Any) -> bool:
    compacted = "".join(str(query or "").lower().split())
    return any(term in compacted for term in ANALYSIS_INTENT_TERMS)


def has_content_task_signal(query: Any) -> bool:
    compacted = "".join(str(query or "").lower().split())
    if "뉴스" in compacted and "뉴스패널" not in compacted:
        return True
    if "news" in compacted and "newspanel" not in compacted:
        return True
    return any(term in compacted for term in CONTENT_TASK_TERMS if term not in {"뉴스", "news"})


def classify_with_provider(
    query: str,
    layout_context: dict[str, Any],
    entity_resolution: dict[str, Any] | None,
    required: bool = False,
):
    classifier = build_intent_classifier_from_env(required=required)
    return classifier.classify(query=query, layout_context=layout_context, entity_resolution=entity_resolution)


def should_call_classifier_fallback(
    *,
    query: str,
    entity_resolution: Any,
    rule_content_tasks: list[Any],
    rule_ui_tasks: list[Any],
    warnings: list[str],
    ui_parser_needs_classifier: bool = False,
    has_subject_fallback: bool = False,
) -> bool:
    if bool_env("AGENT_INTENT_CLASSIFIER_ALWAYS", False):
        return True
    if ui_parser_needs_classifier:
        return not deterministic_understanding_is_sufficient(
            query=query,
            entity_resolution=entity_resolution,
            rule_content_tasks=rule_content_tasks,
            rule_ui_tasks=rule_ui_tasks,
            has_subject_fallback=has_subject_fallback,
        )
    if getattr(entity_resolution, "needs_clarification", False) or getattr(entity_resolution, "status", None) == "ambiguous":
        return not deterministic_understanding_is_sufficient(
            query=query,
            entity_resolution=entity_resolution,
            rule_content_tasks=rule_content_tasks,
            rule_ui_tasks=rule_ui_tasks,
            has_subject_fallback=has_subject_fallback,
        )
    if any(item.endswith("_timeout") for item in warnings):
        return not deterministic_understanding_is_sufficient(
            query=query,
            entity_resolution=entity_resolution,
            rule_content_tasks=rule_content_tasks,
            rule_ui_tasks=rule_ui_tasks,
            has_subject_fallback=has_subject_fallback,
        )
    has_rule_content = bool(rule_content_tasks)
    has_ui = bool(rule_ui_tasks)
    if not has_rule_content and not has_ui:
        return True
    text = str(query or "").strip()
    if text and looks_like_multi_task_query(text) and (len(rule_content_tasks) + len(rule_ui_tasks) < 2):
        return True
    return False


def deterministic_understanding_is_sufficient(
    *,
    query: str,
    entity_resolution: Any,
    rule_content_tasks: list[Any],
    rule_ui_tasks: list[Any],
    has_subject_fallback: bool,
) -> bool:
    has_confirmed_entity = getattr(entity_resolution, "status", None) == "confirmed"
    if has_preset_load_task(rule_ui_tasks):
        return True
    if rule_ui_tasks and not rule_content_tasks:
        return not has_analysis_intent_signal(query)
    if rule_ui_tasks and content_tasks_are_only_panel_references(
        rule_content_tasks,
        rule_ui_tasks,
        has_confirmed_entity,
    ):
        return not has_analysis_intent_signal(query)
    if rule_content_tasks and (has_confirmed_entity or has_subject_fallback):
        return True
    return False


def has_preset_load_task(tasks: list[Any]) -> bool:
    return any(getattr(task, "action", None) == "load" and bool(getattr(task, "presetId", None)) for task in tasks)


def has_subject_fallback(*, request_symbol: Any, chart_context: Any) -> bool:
    if str(request_symbol or "").strip():
        return True
    if not isinstance(chart_context, dict):
        return False
    entity_fallback = chart_context.get("entityFallback")
    if isinstance(entity_fallback, dict) and str(entity_fallback.get("symbol") or "").strip():
        return True
    chart_document = chart_context.get("chartDocument")
    return isinstance(chart_document, dict) and bool(str(chart_document.get("symbol") or "").strip())


def looks_like_multi_task_query(query: str) -> bool:
    text = str(query or "").lower()
    compacted = "".join(text.split())
    separators = ("그리고", "랑", "하고", "동시에", "각각", "또", "및", "and", "&", ",")
    return any(separator in compacted for separator in separators)


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def acquire_llm(runtime_context: Any | None, label: str) -> bool:
    if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
        return bool(runtime_context.acquire_llm(label))
    return True


def topic_from_entity_resolution(entity_resolution: Any) -> dict[str, Any] | None:
    if getattr(entity_resolution, "status", None) != "confirmed":
        return None
    if getattr(entity_resolution, "entity_type", None) != "theme":
        return None
    symbols = [str(symbol).upper() for symbol in getattr(entity_resolution, "theme_symbols", ()) if str(symbol or "").strip()]
    if not symbols:
        return None
    return {
        "label": getattr(entity_resolution, "theme_name", None) or getattr(entity_resolution, "canonical_name", None) or "theme",
        "symbols": tuple(symbols),
        "source": getattr(entity_resolution, "catalog_source", None),
        "entityId": getattr(entity_resolution, "entity_id", None),
    }


def fallback_news_topic(intent: str, explicit_symbol: str | None, entity_resolution: Any) -> dict[str, Any] | None:
    news_topic = topic_from_entity_resolution(entity_resolution)
    if explicit_symbol:
        return None
    if news_topic is None:
        return extract_news_topic_from_intent(intent)
    return news_topic
