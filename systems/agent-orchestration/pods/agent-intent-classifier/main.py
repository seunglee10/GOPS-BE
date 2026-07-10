from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI

from gops_agents.intent_understanding.classifier import ClassifierResult
from gops_agents.intent_understanding.providers.hosted_llm import HostedLlmIntentClassifier
from gops_agents.intent_understanding.rules import deterministic_content_tasks, deterministic_ui_tasks

app = FastAPI(title="GOPS Agent Intent Classifier", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agent-intent-classifier"}


@app.post("/classify")
def classify(request: dict[str, Any]) -> dict[str, Any]:
    query = str(request.get("query") or "")
    layout_context = {}
    if isinstance(request.get("availablePanels"), list):
        layout_context["panels"] = request.get("availablePanels")
    if isinstance(request.get("availablePresets"), list):
        layout_context["presets"] = request.get("availablePresets")
    provider = os.getenv("AGENT_INTENT_CLASSIFIER_POD_PROVIDER", "openai" if os.getenv("OPENAI_API_KEY") else "deterministic").strip().lower()
    result = None
    if provider in {"openai", "hosted", "hosted-llm"}:
        try:
            result = HostedLlmIntentClassifier().classify(
                query=query,
                layout_context=layout_context,
                entity_resolution=request.get("entityResolution") if isinstance(request.get("entityResolution"), dict) else {},
            )
        except Exception as exc:
            result = deterministic_result(query, layout_context)
            result.warnings.append(f"hosted_classifier_failed:{exc.__class__.__name__}")
    if result is None:
        result = deterministic_result(query, layout_context)
    return result.to_dict()


def deterministic_result(query: str, layout_context: dict[str, Any]) -> ClassifierResult:
    content = deterministic_content_tasks(query)
    ui = deterministic_ui_tasks(query, layout_context)
    route_mode = "hybrid" if content and ui else ("ui_layout" if ui else ("analysis" if content else None))
    confidence = max([0.0, *[task.confidence for task in content], *[task.confidence for task in ui]])
    return ClassifierResult(contentTasks=content, uiTasks=ui, routeMode=route_mode, confidence=confidence, source="intent-classifier-deterministic")
