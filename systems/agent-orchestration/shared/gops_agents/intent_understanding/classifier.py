from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from .schema import ContentTask, UiTask


@dataclass
class ClassifierResult:
    contentTasks: list[ContentTask] = field(default_factory=list)
    uiTasks: list[UiTask] = field(default_factory=list)
    routeMode: str | None = None
    confidence: float = 0.0
    source: str = "classifier"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contentTasks": [task.to_dict() for task in self.contentTasks],
            "uiTasks": [task.to_dict() for task in self.uiTasks],
            "routeMode": self.routeMode,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "warnings": list(self.warnings),
        }


class IntentClassifier(Protocol):
    def classify(self, *, query: str, layout_context: dict[str, Any], entity_resolution: dict[str, Any] | None = None) -> ClassifierResult | None:
        ...


class NullIntentClassifier:
    def classify(self, *, query: str, layout_context: dict[str, Any], entity_resolution: dict[str, Any] | None = None) -> ClassifierResult | None:
        return None


def build_intent_classifier_from_env(*, required: bool = False) -> IntentClassifier:
    default_provider = "openai" if required else "deterministic"
    provider = os.getenv("AGENT_INTENT_CLASSIFIER_PROVIDER", default_provider).strip().lower()
    if required and provider in {"", "deterministic", "none", "off", "disabled"}:
        provider = "openai"
    if provider in {"", "deterministic", "none", "off", "disabled"}:
        return NullIntentClassifier()
    if provider in {"openai", "hosted", "hosted-llm"}:
        from .providers.hosted_llm import HostedLlmIntentClassifier

        return HostedLlmIntentClassifier()
    if provider in {"local-http", "http", "pod"}:
        from .providers.local_http import LocalHttpIntentClassifier

        return LocalHttpIntentClassifier()
    return NullIntentClassifier()


def classifier_result_from_payload(payload: Any, *, source: str) -> ClassifierResult | None:
    if not isinstance(payload, dict):
        return None
    content_tasks = []
    for item in payload.get("contentTasks", []):
        if not isinstance(item, dict):
            continue
        content_tasks.append(
            ContentTask(
                taskType=str(item.get("taskType") or item.get("type") or "general"),
                targetEntityText=clean_optional_string(item.get("targetEntityText") or item.get("entityText")),
                confidence=read_confidence(item.get("confidence"), 0.7),
                source=source,
                reason=str(item.get("reason") or "LLM classifier content task."),
            )
        )
    ui_tasks = []
    for item in payload.get("uiTasks", []):
        if not isinstance(item, dict):
            continue
        ui_tasks.append(
            UiTask(
                action=str(item.get("action") or "focus"),
                targetPanelType=clean_optional_string(item.get("targetPanelType") or item.get("panelType")),
                targetPanelId=clean_optional_string(item.get("targetPanelId") or item.get("panelId")),
                targetPanelTypes=clean_string_list(item.get("targetPanelTypes") or item.get("panelTypes")),
                targetPanelIds=clean_string_list(item.get("targetPanelIds") or item.get("panelIds")),
                layoutPreset=clean_optional_string(item.get("layoutPreset") or item.get("preset")),
                presetId=clean_optional_string(item.get("presetId")),
                presetName=clean_optional_string(item.get("presetName")),
                presetKind=clean_optional_string(item.get("presetKind")),
                sizeIntent=clean_optional_string(item.get("sizeIntent") or item.get("size")),
                positionIntent=clean_optional_string(item.get("positionIntent") or item.get("position")),
                confidence=read_confidence(item.get("confidence"), 0.7),
                source=source,
                reason=str(item.get("reason") or "LLM classifier UI task."),
            )
        )
    warnings = [str(item) for item in payload.get("warnings", []) if isinstance(item, str)]
    confidence = read_confidence(payload.get("confidence"), max([0.0, *[task.confidence for task in content_tasks], *[task.confidence for task in ui_tasks]]))
    return ClassifierResult(
        contentTasks=content_tasks,
        uiTasks=ui_tasks,
        routeMode=clean_optional_string(payload.get("routeMode")),
        confidence=confidence,
        source=source,
        warnings=warnings,
    )


def read_confidence(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (clean_optional_string(item) for item in value) if text]
