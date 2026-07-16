from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from gops_agents.orchestration.routing import parse_openai_text_json

from .cache import CompanyCompareNarrativeCache, build_company_compare_cache_from_env, company_compare_cache_key
from .schemas import SECTION_IDS, company_compare_schema


BANNED_LANGUAGE = (
    "매수",
    "매도",
    "추천",
    "더 좋",
    "우위",
    "우월",
    "열등",
    "승자",
    "매력도",
    "투자해야",
)
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?%?")


class CompanyCompareNarrativeError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CompanyCompareNarrativeSynthesizer:
    """저장·계산이 끝난 비교 컨텍스트를 판정 없는 서술로만 변환한다."""

    def __init__(
        self,
        *,
        read_config: Callable[[str], str | None] | None = None,
        response_requester: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        cache: CompanyCompareNarrativeCache | None = None,
    ):
        self.read_config = read_config or os.getenv
        self.response_requester = response_requester or self._request_openai
        self.cache = cache if cache is not None else build_company_compare_cache_from_env()

    def synthesize(self, payload: dict[str, Any]) -> dict[str, Any]:
        quantitative = payload.get("quantitative")
        if not isinstance(quantitative, dict):
            raise CompanyCompareNarrativeError(422, "quantitative context is required.")
        qualitative = payload.get("qualitative")
        if not isinstance(qualitative, dict):
            qualitative = {}
        section_ids = active_section_ids(quantitative, qualitative)
        if not section_ids:
            raise CompanyCompareNarrativeError(422, "No quantitative sections are available for narrative synthesis.")
        evidence_refs = allowed_evidence_refs(quantitative, qualitative, payload.get("sources"))
        if not evidence_refs:
            raise CompanyCompareNarrativeError(422, "No evidence references are available for narrative synthesis.")

        cache_key = company_compare_cache_key(payload)
        cache_token = cache_key.rsplit("rev=", 1)[-1]
        cache_ttl_seconds = positive_int(
            self.read_config("AGENT_COMPANY_COMPARE_CACHE_TTL_SECONDS"),
            86400,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            try:
                validated_cached = validate_narrative(
                    cached,
                    section_ids=section_ids,
                    evidence_refs=evidence_refs,
                )
            except CompanyCompareNarrativeError as exc:
                log_company_compare_event(
                    "cache_invalid",
                    cacheToken=cache_token,
                    errorType=exc.__class__.__name__,
                )
            else:
                log_company_compare_event(
                    "cache_hit",
                    cacheToken=cache_token,
                    sectionCount=len(validated_cached["sections"]),
                )
                return {
                    "status": "ready",
                    **validated_cached,
                    "cache": cache_metadata("hit", cache_ttl_seconds),
                }

        log_company_compare_event("cache_miss", cacheToken=cache_token)
        if not self.read_config("OPENAI_API_KEY"):
            raise CompanyCompareNarrativeError(503, "OpenAI API key is not configured.")

        request_payload = {
            "model": (
                self.read_config("AGENT_COMPANY_COMPARE_MODEL")
                or self.read_config("AGENT_SYNTHESIZER_MODEL")
                or self.read_config("OPENAI_MODEL")
                or "gpt-5.2"
            ),
            "input": [
                {"role": "system", "content": system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps({
                        "baseSymbol": payload.get("baseSymbol"),
                        "compareSymbols": payload.get("compareSymbols") or [],
                        "question": payload.get("question"),
                        "quantitative": quantitative,
                        "qualitative": qualitative,
                        "sources": payload.get("sources") or [],
                        "dataGaps": payload.get("dataGaps") or [],
                        "allowedSectionIds": list(section_ids),
                        "allowedEvidenceRefs": list(evidence_refs),
                    }, ensure_ascii=False),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "company_compare_narrative",
                    "strict": True,
                    "schema": company_compare_schema(
                        section_ids=section_ids,
                        evidence_refs=evidence_refs,
                    ),
                },
            },
        }
        parsed = self.response_requester(request_payload)
        if not isinstance(parsed, dict) or not parsed:
            raise CompanyCompareNarrativeError(502, "OpenAI comparison response did not include JSON output.")
        validated = validate_narrative(parsed, section_ids=section_ids, evidence_refs=evidence_refs)
        unsupported_numbers = find_unsupported_numbers(validated, {
            "quantitative": quantitative,
            "qualitative": qualitative,
        })
        if unsupported_numbers:
            log_company_compare_event(
                "validation_warning",
                cacheToken=cache_token,
                warningType="unsupported_numbers",
                warningCount=len(unsupported_numbers),
            )
        self.cache.set(cache_key, validated, cache_ttl_seconds)
        log_company_compare_event(
            "validation_accepted",
            cacheToken=cache_token,
            sectionCount=len(validated["sections"]),
            warningCount=len(unsupported_numbers),
        )
        return {
            "status": "ready",
            **validated,
            "cache": cache_metadata("miss", cache_ttl_seconds),
        }

    def _request_openai(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self.read_config("OPENAI_API_KEY")
        if not api_key:
            raise CompanyCompareNarrativeError(503, "OpenAI API key is not configured.")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = positive_float(self.read_config("AGENT_COMPANY_COMPARE_TIMEOUT_SECONDS"), 30.0)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = read_openai_error_detail(exc)
            suffix = f" {detail}" if detail else ""
            raise CompanyCompareNarrativeError(
                502,
                f"OpenAI comparison request failed with HTTP {exc.code}.{suffix}",
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = str(getattr(exc, "reason", exc)).strip()[:300]
            suffix = f" ({reason})" if reason else ""
            raise CompanyCompareNarrativeError(
                502,
                f"OpenAI comparison request could not be completed{suffix}.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise CompanyCompareNarrativeError(502, "OpenAI comparison response was not valid JSON.") from exc
        try:
            return parse_openai_text_json(response_data)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CompanyCompareNarrativeError(502, "OpenAI comparison output was not valid structured JSON.") from exc


def system_prompt() -> str:
    return (
        "당신은 GOPS 기업 비교 해석기입니다. 백엔드가 계산해 제공한 근거만 성향 대비 문장으로 바꾸세요. "
        "어느 기업이 더 좋거나 나쁘다는 판정, 점수, 순위, 승자, 매수·매도·추천 등 투자권유를 절대 작성하지 마세요. "
        "금지 표현은 면책 문장에서도 그대로 반복하지 말고 '정보성 분석이며 투자 판단을 대신하지 않습니다'처럼 중립적으로 쓰세요. "
        "제공되지 않은 수치, 사실, 원인, 전망을 만들지 말고 모든 섹션의 evidenceRefs에는 allowedEvidenceRefs 값만 넣으세요. "
        "allowedSectionIds의 각 섹션을 제공된 순서대로 정확히 한 번씩 작성하고 그 밖의 섹션은 만들지 마세요. "
        "데이터가 부족한 내용은 dataGaps에 짧게 적으세요. 10-K의 severityHint는 회사 간 우열 점수가 아니라 문서 강조도 보조값입니다. "
        "summary와 analysis는 사용자의 언어로 작성하되 질문 언어가 불명확하면 한국어를 사용하세요. "
        "summary에는 이 결과가 정보성 분석이며 투자 판단을 대신하지 않는다는 점을 자연스럽게 명시하세요. "
        "고정된 정량 사실을 다시 계산하지 말고, 차이가 드러나는 방식과 각 회사의 성향만 중립적으로 설명하세요."
    )


def active_section_ids(
    quantitative: dict[str, Any],
    qualitative: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    raw_ids = [
        str(section.get("id") or "")
        for section in [
            *(quantitative.get("sections") or []),
            *((qualitative or {}).get("sections") or []),
        ]
        if isinstance(section, dict)
    ]
    return tuple(section_id for section_id in SECTION_IDS if section_id in raw_ids)


def allowed_evidence_refs(
    quantitative: dict[str, Any],
    qualitative: dict[str, Any],
    sources: Any,
) -> tuple[str, ...]:
    refs: list[str] = []
    for source in sources or []:
        if isinstance(source, dict):
            append_unique(refs, source.get("id"))
    for section in quantitative.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for metric in section.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            for value in metric.get("values") or []:
                if isinstance(value, dict):
                    append_unique(refs, value.get("sourceRef"))
    for section in qualitative.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for reference in section.get("evidenceRefs") or []:
            append_unique(refs, reference)
        for item in section.get("items") or []:
            if isinstance(item, dict):
                append_unique(refs, item.get("sourceRef"))
    return tuple(refs)


def validate_narrative(
    payload: dict[str, Any],
    *,
    section_ids: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> dict[str, Any]:
    required = ("summary", "sections", "insights", "dataGaps")
    if any(key not in payload for key in required):
        raise CompanyCompareNarrativeError(502, "OpenAI comparison output was missing required fields.")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise CompanyCompareNarrativeError(502, "OpenAI comparison output summary was empty.")
    text = " ".join(iter_narrative_text(payload))
    violation = next((term for term in BANNED_LANGUAGE if term in text), "")
    if violation:
        raise CompanyCompareNarrativeError(502, f"OpenAI comparison output contained prohibited language: {violation}")

    seen: set[str] = set()
    clean_sections: list[dict[str, Any]] = []
    allowed_sections = set(section_ids)
    allowed_refs = set(evidence_refs)
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            raise CompanyCompareNarrativeError(502, "OpenAI comparison section was invalid.")
        section_id = str(section.get("id") or "")
        if section_id not in allowed_sections or section_id in seen:
            raise CompanyCompareNarrativeError(502, "OpenAI comparison section id was invalid or duplicated.")
        refs = [str(value) for value in section.get("evidenceRefs") or []]
        if any(value not in allowed_refs for value in refs):
            raise CompanyCompareNarrativeError(502, "OpenAI comparison output referenced unknown evidence.")
        heading = str(section.get("heading") or "").strip()
        analysis = str(section.get("analysis") or "").strip()
        if not heading or not analysis:
            raise CompanyCompareNarrativeError(502, "OpenAI comparison section text was empty.")
        if not refs:
            raise CompanyCompareNarrativeError(502, "OpenAI comparison section did not cite evidence.")
        seen.add(section_id)
        clean_sections.append({
            "id": section_id,
            "heading": heading,
            "analysis": analysis,
            "evidenceRefs": refs,
        })
    missing_sections = [section_id for section_id in section_ids if section_id not in seen]
    if missing_sections:
        raise CompanyCompareNarrativeError(
            502,
            f"OpenAI comparison output omitted required sections: {', '.join(missing_sections)}",
        )
    return {
        "summary": summary,
        "sections": clean_sections,
        "insights": [str(value).strip() for value in payload.get("insights") or [] if str(value).strip()],
        "dataGaps": [str(value).strip() for value in payload.get("dataGaps") or [] if str(value).strip()],
    }


def find_unsupported_numbers(narrative: dict[str, Any], quantitative: dict[str, Any]) -> list[str]:
    allowed: set[str] = set()
    for value in walk_values(quantitative):
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            allowed.update(number_variants(value))
        elif isinstance(value, str):
            allowed.update(normalize_number(token) for token in NUMBER_PATTERN.findall(value))
    found: list[str] = []
    for text in iter_narrative_text(narrative):
        for token in NUMBER_PATTERN.findall(text):
            normalized = normalize_number(token)
            if normalized and normalized not in allowed and normalized not in found:
                found.append(normalized)
    return found


def iter_narrative_text(payload: dict[str, Any]):
    yield str(payload.get("summary") or "")
    for section in payload.get("sections") or []:
        if isinstance(section, dict):
            yield str(section.get("heading") or "")
            yield str(section.get("analysis") or "")
    for key in ("insights", "dataGaps"):
        for value in payload.get(key) or []:
            yield str(value)


def walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)
    else:
        yield value


def number_variants(value: int | float) -> set[str]:
    numeric = float(value)
    variants = {
        normalize_number(str(value)),
        normalize_number(f"{numeric:.1f}"),
        normalize_number(f"{numeric:.2f}"),
        normalize_number(f"{numeric * 100:.1f}%"),
        normalize_number(f"{numeric * 100:.2f}%"),
    }
    return {variant for variant in variants if variant}


def normalize_number(value: str) -> str:
    return str(value).replace(",", "").lstrip("+").strip()


def append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(value) if value else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value) if value else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def cache_metadata(status: str, ttl_seconds: int) -> dict[str, Any]:
    return {
        "status": status,
        "version": "company-compare-cache.v1",
        "ttlSeconds": ttl_seconds,
    }


def log_company_compare_event(event: str, **fields: Any) -> None:
    print(
        json.dumps(
            {"event": f"company_compare_narrative_{event}", **fields},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def read_openai_error_detail(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8")
        parsed = json.loads(body)
    except Exception:
        return ""
    detail = parsed.get("error") if isinstance(parsed, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else None
    return str(message or "").strip()[:600]
