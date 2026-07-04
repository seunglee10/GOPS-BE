from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from ...orchestration.ui_intent import compact_layout_panels
from ..classifier import ClassifierResult, classifier_result_from_payload


class LocalHttpIntentClassifier:
    def __init__(self, url: str | None = None):
        self.url = (url or os.getenv("AGENT_INTENT_CLASSIFIER_URL") or "http://agent-intent-classifier:8120/classify").rstrip("/")

    def classify(self, *, query: str, layout_context: dict[str, Any], entity_resolution: dict[str, Any] | None = None) -> ClassifierResult | None:
        payload = {
            "query": query,
            "entityResolution": entity_resolution or {},
            "availablePanels": compact_layout_panels(layout_context if isinstance(layout_context, dict) else {}),
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = float(os.getenv("AGENT_INTENT_CLASSIFIER_TIMEOUT_SECONDS", "2.5"))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        return classifier_result_from_payload(parsed, source="intent-classifier-local-http")
