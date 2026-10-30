"""요약 생성기가 쓰는 LLM 경계.

다른 에이전트(chart_assets.commentary, providers.news_localization)와 같은 방식으로
OpenAI Responses API 를 urllib 로 직접 부르고, `text.format=json_schema(strict)` 로
JSON 만 받는다. 테스트는 `JsonModelClient` 를 가짜로 바꿔 네트워크에 나가지 않는다.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

from ..orchestration.routing import parse_openai_text_json

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
TRUE_VALUES = {"1", "true", "yes", "on"}


class SummaryModelError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class JsonModelClient(Protocol):
    """instructions + payload 를 주면 schema 에 맞는 dict 하나를 돌려준다. 실패하면 SummaryModelError."""

    model: str

    def generate_json(
        self,
        *,
        name: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...


class OpenAIResponsesJsonClient:
    def __init__(
        self,
        *,
        read_config: Callable[[str], str | None] | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self.read_config = read_config or os.getenv
        self.model = (
            self.read_config("STOCK_SUMMARY_MODEL")
            or self.read_config("OPENAI_MODEL")
            or "gpt-5.2"
        ).strip()
        self.urlopen = urlopen or urllib.request.urlopen

    def generate_json(
        self,
        *,
        name: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        api_key = (self.read_config("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise SummaryModelError("OPENAI_API_KEY is not configured", code="provider_config")
        body = {
            "model": self.model,
            "store": False,
            "instructions": instructions,
            "input": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
        }
        request = urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        timeout = _positive_float(self.read_config("STOCK_SUMMARY_TIMEOUT_SECONDS"), 30.0)
        try:
            with self.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SummaryModelError(f"OpenAI request failed with HTTP {exc.code}", code="provider_http") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise SummaryModelError("OpenAI request timed out", code="provider_timeout") from exc
        except json.JSONDecodeError as exc:
            raise SummaryModelError("OpenAI response was not JSON", code="output_parse") from exc
        if data.get("status") not in {None, "completed"} or data.get("incomplete_details"):
            raise SummaryModelError("OpenAI response was incomplete", code="provider_incomplete")
        try:
            parsed = parse_openai_text_json(data)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SummaryModelError("OpenAI output was not valid JSON", code="output_parse") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise SummaryModelError("OpenAI output was empty", code="output_parse")
        return parsed


def summary_llm_enabled(read_config: Callable[[str], str | None] | None = None) -> bool:
    read = read_config or os.getenv
    return str(read("STOCK_SUMMARY_LLM_ENABLED") or "false").strip().lower() in TRUE_VALUES


def build_summary_model_client_from_env(
    read_config: Callable[[str], str | None] | None = None,
) -> JsonModelClient | None:
    """STOCK_SUMMARY_LLM_ENABLED=true 이고 키가 있을 때만 모델을 쓴다. 아니면 None(템플릿만)."""
    read = read_config or os.getenv
    if not summary_llm_enabled(read):
        return None
    if not (read("OPENAI_API_KEY") or "").strip():
        return None
    return OpenAIResponsesJsonClient(read_config=read)


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
