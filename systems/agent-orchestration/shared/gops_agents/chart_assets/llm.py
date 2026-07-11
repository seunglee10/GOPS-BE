from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .curation import (
    PROMPT_VERSION_V2,
    curation_output_schema,
    deterministic_curation,
    validate_curation_output,
)


SYSTEM_PROMPT = (
    "검증된 차트 후보의 ID만 선택하는 큐레이터입니다. "
    "좌표, 가격, 문장, 도구를 만들지 말고 strict JSON으로 답하세요. "
    "선택 0개도 유효하며 품질보다 수량을 우선하지 마세요."
)


class ChartAssetLLMService:
    """One bounded symbol-level curator call; geometry stays kernel-owned."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        opener: Callable[..., Any] | None = None,
        max_concurrency: int = 4,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        self.model = model or os.getenv("CHART_ASSET_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.2"
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _env_float("CHART_ASSET_LLM_TIMEOUT_SECONDS", 25.0)
        self.enabled = enabled if enabled is not None else _env_enabled("CHART_ASSET_LLM_ENABLED", True)
        self.opener = opener or urllib.request.urlopen
        self.semaphore = threading.BoundedSemaphore(max(1, max_concurrency))
        self.sleeper = sleeper or time.sleep

    def curate_symbol(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return self._degraded(bundle, "llm_disabled", model=None)
        if not self.api_key:
            return self._degraded(bundle, "missing_openai_api_key", model=self.model)

        request_payload = {
            "model": self.model,
            "store": False,
            "max_output_tokens": 1200,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "chart_asset_curation_v2",
                    "strict": True,
                    "schema": curation_output_schema(),
                }
            },
        }
        last_reason = "openai_failed"
        for attempt in range(2):
            try:
                started = time.perf_counter()
                with self.semaphore:
                    data = self._request_payload(request_payload)
                if str(data.get("status") or "completed") != "completed" or data.get("incomplete_details"):
                    raise ValueError("incomplete_response")
                output = validate_curation_output(json.loads(_openai_output_text(data)), bundle)
                return {
                    "output": output,
                    "degraded": False,
                    "reason": None,
                    "model": data.get("model") or self.model,
                    "usage": data.get("usage") or {},
                    "latencyMs": round((time.perf_counter() - started) * 1000),
                    "promptVersion": PROMPT_VERSION_V2,
                }
            except Exception as exc:
                last_reason = f"openai_http_{exc.code}" if isinstance(exc, urllib.error.HTTPError) else f"openai_{exc.__class__.__name__}"
                if attempt == 0 and _needs_backoff(exc):
                    self.sleeper(0.5)
                    continue
                break
        return self._degraded(bundle, last_reason, model=self.model)

    def _request_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with self.opener(request, timeout=max(0.001, self.timeout_seconds)) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("OpenAI response is not an object")
        return value

    @staticmethod
    def _degraded(bundle: dict[str, Any], reason: str, *, model: str | None) -> dict[str, Any]:
        return {
            "output": deterministic_curation(bundle),
            "degraded": True,
            "reason": reason,
            "model": model,
            "usage": {},
            "promptVersion": PROMPT_VERSION_V2,
        }


def _openai_output_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"]
    for output in data.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise ValueError("OpenAI response did not include output text")


def _env_enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _needs_backoff(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, urllib.error.URLError)) or (
        isinstance(exc, urllib.error.HTTPError) and (exc.code == 429 or 500 <= exc.code < 600)
    )
