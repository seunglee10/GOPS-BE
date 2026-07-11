from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .compilers import fallback_commentary
from .intent_compiler import compile_agent_layer, validate_output_shape
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_llm_input, chart_asset_output_schema
from .curation import (
    PROMPT_VERSION_V2, curation_output_schema, deterministic_curation,
    validate_curation_output,
)


NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")


class ChartAssetLLMService:
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
            return {"output": deterministic_curation(bundle), "degraded": True, "reason": "llm_disabled", "model": None, "usage": {}}
        if not self.api_key:
            return {"output": deterministic_curation(bundle), "degraded": True, "reason": "missing_openai_api_key", "model": self.model, "usage": {}}
        request_payload = {
            "model": self.model, "store": False, "max_output_tokens": 1200,
            "input": [
                {"role": "system", "content": "검증된 차트 후보의 ID만 선택하는 큐레이터입니다. 좌표, 가격, 문장, 도구를 만들지 말고 strict JSON으로 답하세요. 선택 0개도 유효하며 품질보다 수량을 우선하지 마세요."},
                {"role": "user", "content": json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))},
            ],
            "text": {"format": {"type": "json_schema", "name": "chart_asset_curation_v2", "strict": True, "schema": curation_output_schema()}},
        }
        last_reason = "openai_failed"
        for attempt in range(2):
            try:
                started = time.perf_counter()
                with self.semaphore:
                    data = self._request_payload(request_payload)
                status = str(data.get("status") or "completed")
                if status != "completed" or data.get("incomplete_details"):
                    raise ValueError("incomplete_response")
                output = validate_curation_output(json.loads(_openai_output_text(data)), bundle)
                return {"output": output, "degraded": False, "reason": None, "model": data.get("model") or self.model, "usage": data.get("usage") or {}, "latencyMs": round((time.perf_counter()-started)*1000), "promptVersion": PROMPT_VERSION_V2}
            except Exception as exc:
                last_reason = f"openai_{exc.__class__.__name__}"
                if attempt == 0 and _needs_backoff(exc):
                    self.sleeper(.5)
                    continue
                break
        return {"output": deterministic_curation(bundle), "degraded": True, "reason": last_reason, "model": self.model, "usage": {}, "promptVersion": PROMPT_VERSION_V2}

    def build(
        self,
        *,
        symbol: str,
        interval: str,
        candles: list[dict[str, Any]],
        features: dict[str, Any],
        rule_layers: dict[str, Any],
        higher_assets: dict[str, dict[str, Any]],
        generated_at: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            return self._degraded(symbol, interval, features, candles, "llm_disabled")
        if not self.api_key:
            return self._degraded(symbol, interval, features, candles, "missing_openai_api_key")
        prompt_input = build_llm_input(
            symbol=symbol,
            interval=interval,
            candles=candles,
            features=features,
            rule_layers=rule_layers,
            higher_assets=higher_assets,
        )
        last_reason = "openai_failed"
        output: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                with self.semaphore:
                    output = validate_output_shape(self._request(prompt_input))
                break
            except Exception as exc:
                last_reason = f"openai_{exc.__class__.__name__}"
                if attempt == 0 and _needs_backoff(exc):
                    self.sleeper(0.5 * (2 ** attempt))
        if output is None:
            return self._degraded(symbol, interval, features, candles, last_reason)
        agent_layer = compile_agent_layer(
            symbol=symbol,
            interval=interval,
            intents=output["intents"],
            features=features,
            rule_layers=rule_layers,
            generated_at=generated_at,
            model=self.model,
        )
        flags = grounding_flags(output["commentary"]["text"], prompt_input)
        agent_layer["meta"]["groundingFlags"] = flags
        commentary = dict(output["commentary"])
        commentary["confidence"] = float(commentary["confidence"])
        commentary["enrichment"] = None
        return {
            "agentLayer": agent_layer,
            "indicatorSuggestions": output["indicatorSuggestions"],
            "commentary": commentary,
            "promptVersion": PROMPT_VERSION,
        }

    def _request(self, prompt_input: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt_input, ensure_ascii=False, separators=(",", ":"))},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "chart_analysis_asset_layer",
                    "strict": True,
                    "schema": chart_asset_output_schema(),
                }
            },
        }
        return self._request_payload(payload)

    def _request_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with self.opener(request, timeout=max(0.001, self.timeout_seconds)) as response:
            data = json.loads(response.read().decode("utf-8"))
        if payload.get("text", {}).get("format", {}).get("name") == "chart_asset_curation_v2":
            return data
        text = _openai_output_text(data)
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("OpenAI chart asset output is not an object")
        return parsed

    def _degraded(
        self,
        symbol: str,
        interval: str,
        features: dict[str, Any],
        candles: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        return degraded_result(
            symbol=symbol,
            interval=interval,
            features=features,
            candles=candles,
            reason=reason,
            model=self.model if self.enabled else None,
        )


def degraded_result(
    *,
    symbol: str,
    interval: str,
    features: dict[str, Any],
    candles: list[dict[str, Any]],
    reason: str,
    model: str | None,
) -> dict[str, Any]:
    commentary = fallback_commentary(symbol, interval, features, float(candles[-1]["close"]))
    return {
        "agentLayer": {
            "drawings": [], "intents": [], "rationale": "", "degraded": True,
            "model": model, "droppedIntents": [],
            "meta": {"failureReason": reason, "groundingFlags": []},
        },
        "indicatorSuggestions": [],
        "commentary": commentary,
        "promptVersion": PROMPT_VERSION,
    }


def grounding_flags(commentary_text: str, prompt_input: dict[str, Any]) -> list[str]:
    input_numbers = {_normalize_number(value) for value in NUMBER_PATTERN.findall(json.dumps(prompt_input, ensure_ascii=False))}
    commentary_numbers = {_normalize_number(value) for value in NUMBER_PATTERN.findall(commentary_text)}
    input_numbers.discard(None)
    commentary_numbers.discard(None)
    return ["ungrounded_number"] if commentary_numbers.difference(input_numbers) else []


def _normalize_number(value: str) -> str | None:
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


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
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _needs_backoff(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and (exc.code == 429 or 500 <= exc.code < 600)
