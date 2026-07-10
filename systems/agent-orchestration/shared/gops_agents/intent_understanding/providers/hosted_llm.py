from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from ...orchestration.routing import parse_openai_text_json
from ...orchestration.ui_intent import compact_layout_panels
from ..ui_parser import compact_layout_presets_for_parser
from ..classifier import ClassifierResult, classifier_result_from_payload
from ..schema import CONTENT_TASK_TYPES, ROUTE_MODES, UI_ACTIONS, UI_PANEL_TYPES, UI_POSITION_INTENTS, UI_SIZE_INTENTS


class HostedLlmIntentClassifier:
    def classify(self, *, query: str, layout_context: dict[str, Any], entity_resolution: dict[str, Any] | None = None) -> ClassifierResult | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        payload = {
            "model": os.getenv("AGENT_INTENT_CLASSIFIER_MODEL", os.getenv("AGENT_ROUTER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2"))),
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Parse one Korean stock-app query into independent content tasks and UI layout tasks. "
                        "Handle typos and informal wording. Return strict JSON only. "
                        "For multi-panel UI commands, return one uiTask with targetPanelTypes or layoutPreset. "
                        "Use layoutPreset=default_workspace when the user asks for several panels without naming them. "
                        "For preset load requests, use action=load and choose presetId only from availablePresets. "
                        "Do not invent ticker symbols; put unresolved names in targetEntityText."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "query": query,
                            "entityResolution": entity_resolution or {},
                            "availablePanels": compact_layout_panels(layout_context if isinstance(layout_context, dict) else {}),
                            "availablePresets": compact_layout_presets_for_parser(layout_context if isinstance(layout_context, dict) else {}),
                            "contentTaskTypes": CONTENT_TASK_TYPES,
                            "uiActions": UI_ACTIONS,
                            "uiPanelTypes": UI_PANEL_TYPES,
                            "uiSizeIntents": UI_SIZE_INTENTS,
                            "uiPositionIntents": UI_POSITION_INTENTS,
                            "routeModes": ROUTE_MODES,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "query_understanding",
                    "strict": False,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "contentTasks": {"type": "array"},
                            "uiTasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string"},
                                        "targetPanelType": {"type": "string"},
                                        "targetPanelId": {"type": "string"},
                                        "targetPanelTypes": {"type": "array"},
                                        "targetPanelIds": {"type": "array"},
                                        "layoutPreset": {"type": "string"},
                                        "presetId": {"type": "string"},
                                        "presetName": {"type": "string"},
                                        "presetKind": {"type": "string"},
                                        "sizeIntent": {"type": "string"},
                                        "positionIntent": {"type": "string"},
                                        "confidence": {"type": "number"},
                                        "reason": {"type": "string"},
                                    },
                                },
                            },
                            "routeMode": {"type": "string"},
                            "confidence": {"type": "number"},
                            "warnings": {"type": "array"},
                        },
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
        with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_INTENT_CLASSIFIER_TIMEOUT_SECONDS", "2.5"))) as response:
            data = json.loads(response.read().decode("utf-8"))
        parsed = parse_openai_text_json(data)
        return classifier_result_from_payload(parsed, source="intent-classifier-openai")
