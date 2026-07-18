import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.contracts.chart import AgentChatRequest
from app.contracts.related_indices import RelatedIndexCommentaryRequest
from app.core.config import read_dotenv_value
from app.market_data.indices.service import INDEX_DEFINITIONS
from app.services.alfaka_market_data import configured_symbols, get_market_data_provider, normalize_market_symbol
from gops_agents.chart_command import (
    ANALYSIS_KEYWORDS,
    ANALYSIS_TIMEFRAMES,
    ChartCommandAgent,
    ChartCommandError,
    chart_context_for_agent_prompt,
    extract_openai_error_detail,
    extract_response_text,
    is_chart_analysis_request,
    is_live_feed_status_request,
)
from gops_agents.chart_command import (
    build_agent_market_analysis_context as _build_agent_market_analysis_context,
)
from gops_agents.chart_command import (
    request_openai_response as _request_openai_response,
)


def _agent() -> ChartCommandAgent:
    return ChartCommandAgent(
        read_config=read_dotenv_value,
        configured_symbols=configured_symbols,
        response_requester=request_openai_response,
    )


def _map_chart_command_error(error: ChartCommandError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


def openai_agent_chat(request: AgentChatRequest) -> dict[str, Any]:
    try:
        return _agent().chat(request)
    except ChartCommandError as exc:
        raise _map_chart_command_error(exc) from exc


def openai_chart_proposal(context: dict[str, Any]) -> dict[str, Any]:
    try:
        return _agent().chart_proposal(context)
    except ChartCommandError as exc:
        raise _map_chart_command_error(exc) from exc


def request_openai_response(payload: dict[str, Any]) -> str:
    try:
        return _request_openai_response(payload, read_config=read_dotenv_value)
    except ChartCommandError as exc:
        raise _map_chart_command_error(exc) from exc


RELATED_INDEX_COMMENTARY_TITLE = "왜 이 지수를 보여줬나요?"
RELATED_INDEX_COMMENTARY_CACHE_SECONDS = 172_800
RELATED_INDEX_COMMENTARY_FAILURE_CACHE_SECONDS = 300
RELATED_INDEX_COMMENTARY_TIMEZONE = ZoneInfo("America/New_York")
RELATED_INDEX_BANNED_PHRASES = (
    "체온계",
    "숨은 변수",
    "무려",
    "크게",
    "해보세요",
    "함께 보세요",
    "AI가 분석한",
    "제가",
)
_related_index_commentary_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_related_index_commentary_cache_lock = threading.Lock()


def openai_related_index_commentary(request: RelatedIndexCommentaryRequest) -> dict[str, Any]:
    normalized_symbol = normalize_market_symbol(request.symbol)
    allowed_indices = {item["symbol"] for item in INDEX_DEFINITIONS}
    if request.indexSymbol not in allowed_indices:
        return related_index_template_fallback(request.templateBody)

    trading_date = datetime.now(RELATED_INDEX_COMMENTARY_TIMEZONE).date().isoformat()
    cache_key = related_index_commentary_cache_key(normalized_symbol, request.indexSymbol, trading_date)
    cached = related_index_commentary_cache_get(cache_key)
    if cached is not None:
        return cached

    payload_data = request.model_dump()
    payload_data["symbol"] = normalized_symbol
    last_error = ""
    for attempt in range(2):
        system_prompt = related_index_commentary_system_prompt()
        if attempt:
            system_prompt += f" 이전 출력 검증 오류: {last_error}. 모든 제약을 바로잡아 다시 출력한다."
        response_payload = {
            "model": read_dotenv_value("RELATED_INDEX_COMMENTARY_MODEL") or read_dotenv_value("OPENAI_MODEL") or "gpt-5.2",
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload_data, ensure_ascii=False)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "related_index_commentary",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["body"],
                        "properties": {"body": {"type": "string", "maxLength": 100}},
                    },
                },
            },
        }
        try:
            raw = request_openai_response(response_payload)
            parsed = json.loads(raw)
            body = str(parsed.get("body") or "").strip() if isinstance(parsed, dict) else ""
            last_error = validate_related_index_commentary_body(body) or ""
            if not last_error:
                result = {
                    "title": RELATED_INDEX_COMMENTARY_TITLE,
                    "body": body,
                    "source": "llm",
                    "generatedAt": utc_timestamp(),
                }
                related_index_commentary_cache_set(cache_key, result, RELATED_INDEX_COMMENTARY_CACHE_SECONDS)
                return result
        except Exception as exc:
            last_error = exc.__class__.__name__

    fallback = related_index_template_fallback(request.templateBody)
    related_index_commentary_cache_set(cache_key, fallback, RELATED_INDEX_COMMENTARY_FAILURE_CACHE_SECONDS)
    return fallback


def related_index_commentary_system_prompt() -> str:
    return (
        "GOPS 기업 관련 지수 툴팁의 한국어 본문을 JSON으로 작성한다. "
        "본문은 정확히 두 문장, 전체 100자 이하이며 각 문장은 평서형 -다.로 끝난다. "
        "첫 문장은 수치와 제공된 근거에 기반한 관계 사실, 둘째 문장은 오늘 움직임의 함의를 쓴다. "
        "evidence 칩에 있는 수치를 본문에서 반복하지 않는다. "
        "수급, 편입 비중, 상관계수, 밸류에이션, 할인율, 초과 상승은 사용할 수 있다. "
        "요., 이에요., 예요., 죠., 권유형, 은유, 의인화, 과장 부사, 감탄, 이모지, AI 자기지칭을 금지한다. "
        "불확실성 표현은 한 문장에 한 번만 사용한다. 제공되지 않은 사실이나 숫자는 만들지 않는다. "
        "예시1: 미국 반도체 30종목 업종 지수다. 경쟁사와 공급망이 함께 편입돼 업황 지표로 유효하다. "
        "예시2: 성장주 밸류에이션은 금리와 할인율에 민감하다. 최근 상관은 역방향 경향을 시사한다."
    )


def validate_related_index_commentary_body(body: str) -> str | None:
    if not body:
        return "본문이 비어 있음"
    if len(body) > 100:
        return "100자 초과"
    if re.search(r"(?:요|죠)\.", body):
        return "금지 종결어미 사용"
    if any(phrase in body for phrase in RELATED_INDEX_BANNED_PHRASES):
        return "금지 표현 사용"
    if not re.fullmatch(r"[^.!?]+다\.\s*[^.!?]+다\.", body):
        return "정확히 두 문장의 -다체가 아님"
    return None


def related_index_template_fallback(body: str) -> dict[str, Any]:
    return {
        "title": RELATED_INDEX_COMMENTARY_TITLE,
        "body": body,
        "source": "template",
        "generatedAt": utc_timestamp(),
    }


def related_index_commentary_cache_key(symbol: str, index_symbol: str, trading_date: str) -> str:
    safe_index = index_symbol.replace("^", "").replace("=", "-").replace(".", "-")
    prefix = (os.getenv("REDIS_KEY_PREFIX") or "gops:market:on-demand:v1").strip().strip(":")
    suffix = f"llm:related-index:{symbol}:{safe_index}:{trading_date}"
    return f"{prefix}:{suffix}" if prefix else suffix


def related_index_commentary_cache_get(key: str) -> dict[str, Any] | None:
    with _related_index_commentary_cache_lock:
        cached = _related_index_commentary_cache.get(key)
        if cached is not None:
            expires_at, payload = cached
            if expires_at > time.monotonic():
                return dict(payload)
            _related_index_commentary_cache.pop(key, None)
    redis_client = related_index_commentary_redis()
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(key)
    except Exception:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    memory_ttl = (
        RELATED_INDEX_COMMENTARY_CACHE_SECONDS
        if parsed.get("source") == "llm"
        else RELATED_INDEX_COMMENTARY_FAILURE_CACHE_SECONDS
    )
    with _related_index_commentary_cache_lock:
        _related_index_commentary_cache[key] = (time.monotonic() + memory_ttl, dict(parsed))
    return parsed


def related_index_commentary_cache_set(key: str, payload: dict[str, Any], ttl: int) -> None:
    with _related_index_commentary_cache_lock:
        _related_index_commentary_cache[key] = (time.monotonic() + ttl, dict(payload))
    redis_client = related_index_commentary_redis()
    if redis_client is None:
        return
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        redis_client.set(key, encoded, ex=ttl)
    except TypeError:
        try:
            redis_client.set(key, encoded)
            redis_client.expire(key, ttl)
        except Exception:
            return
    except Exception:
        return


def related_index_commentary_redis() -> Any | None:
    try:
        provider = get_market_data_provider()
    except Exception:
        return None
    redis_provider = getattr(provider, "redis_provider", None)
    return getattr(redis_provider, "redis", None)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_agent_market_analysis_context(context: dict[str, Any]) -> dict[str, Any]:
    return _build_agent_market_analysis_context(context, configured_symbols=configured_symbols)
