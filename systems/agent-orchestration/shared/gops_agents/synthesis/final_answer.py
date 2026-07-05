from __future__ import annotations

import json
import os
import re
import hashlib
import urllib.request
from collections import Counter
from typing import Any

from ..contracts import (
    AgentAnswer,
    AgentFinding,
    EvidenceItem,
    FinalAnswer,
    FinalAnswerCitation,
    FinalAnswerSection,
    IntentRoute,
    SynthesisInput,
)
from ..orchestration.routing import parse_openai_text_json
from ..runtime.analysis_cache import final_answer_from_dict
from ..security import sanitize_text, sanitize_url, sanitize_value


class FinalAnswerSynthesizer:
    def synthesize(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        timing: dict[str, Any] | None = None,
        daily_summaries: list[dict[str, Any]] | None = None,
        synthesis_input: SynthesisInput | None = None,
        runtime_context: Any | None = None,
    ) -> FinalAnswer:
        if is_news_route(route):
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                daily_summaries=daily_summaries,
            )
        if is_ontology_route(route) and os.getenv("AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER") != "openai":
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
            )
        if is_financial_route(route) and os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_PROVIDER") != "openai":
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
            )
        if is_financial_route(route):
            openai_answer = self._synthesize_financial_with_openai(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                timing=timing,
                synthesis_input=synthesis_input,
                runtime_context=runtime_context,
            )
        else:
            openai_answer = self._synthesize_with_openai(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                timing=timing,
                synthesis_input=synthesis_input,
                runtime_context=runtime_context,
            )
        if openai_answer:
            return openai_answer
        return self._synthesize_deterministic(
            symbol=symbol,
            intent=intent,
            route=route,
            findings=findings,
            provider_evidence=provider_evidence,
        )

    def _synthesize_deterministic(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        daily_summaries: list[dict[str, Any]] | None = None,
    ) -> FinalAnswer:
        intent_types = {part.strip() for part in str(route.intentType or "").lower().split("+") if part.strip()}
        if route.intentType == "news" or route.selectedRoles == ["news"]:
            return sanitize_final_answer(build_news_final_answer(symbol, findings, provider_evidence, daily_summaries=daily_summaries))
        if route.intentType == "ontology" or route.selectedRoles == ["ontology"]:
            return sanitize_final_answer(build_ontology_final_answer(symbol, findings, provider_evidence))
        if is_financial_route(route):
            return sanitize_final_answer(build_financial_final_answer(symbol, findings, provider_evidence))
        if "market-move" in intent_types:
            return sanitize_final_answer(build_market_move_final_answer(symbol, findings, provider_evidence))
        return sanitize_final_answer(build_general_final_answer(symbol, route, findings, provider_evidence))

    def _synthesize_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        timing: dict[str, Any] | None = None,
        synthesis_input: SynthesisInput | None = None,
        runtime_context: Any | None = None,
    ) -> FinalAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key or os.getenv("AGENT_FINAL_ANSWER_PROVIDER") == "deterministic":
            return None
        if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
            if not runtime_context.acquire_llm("synthesis"):
                return None
        try:
            if runtime_context is None and isinstance(timing, dict):
                timing["llmCalls"] = int(timing.get("llmCalls") or 0) + 1
            payload = {
                "model": os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You generate stock-analysis answers from retrieved evidence only. "
                            "Do not invent facts, prices, news, macro data, relationships, citations, or recommendations. "
                            "Write Korean report-style prose. Hide internal route, provider, and guardrail diagnostics from users. "
                            "Never mention JSON field names such as providerEvidence, findings, route, selectedRoles, or guardrail. "
                            "If evidence is missing, put it in limitations. Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            synthesis_payload(
                                symbol=symbol,
                                intent=intent,
                                route=route,
                                findings=findings,
                                provider_evidence=provider_evidence,
                                synthesis_input=synthesis_input,
                            ),
                            ensure_ascii=False,
                        ),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "agent_final_answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "sections": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string"},
                                            "bullets": {"type": "array", "items": {"type": "string"}},
                                        },
                                        "required": ["title", "bullets"],
                                    },
                                },
                                "citations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "provider": {"type": "string"},
                                            "title": {"type": "string"},
                                            "url": {"type": ["string", "null"]},
                                            "publishedAt": {"type": ["string", "null"]},
                                        },
                                        "required": ["provider", "title", "url", "publishedAt"],
                                    },
                                },
                                "limitations": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["title", "summary", "sections", "citations", "limitations"],
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
            timeout_seconds = float(os.getenv("AGENT_SYNTHESIZER_TIMEOUT_SECONDS", "0"))
            if timeout_seconds <= 0 and runtime_context is not None:
                timeout_seconds = max(0.001, float(getattr(runtime_context.policy, "synthesis_timeout_ms", 1700)) / 1000)
            if timeout_seconds <= 0:
                timeout_seconds = 12.0
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            return final_answer_from_openai_json(parse_openai_text_json(data))
        except Exception:
            return None

    def _synthesize_financial_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        timing: dict[str, Any] | None = None,
        synthesis_input: SynthesisInput | None = None,
        runtime_context: Any | None = None,
    ) -> FinalAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key or os.getenv("AGENT_FINAL_ANSWER_PROVIDER") == "deterministic":
            return None
        payload_data = financial_synthesis_payload(
            symbol=symbol,
            intent=intent,
            route=route,
            findings=findings,
            provider_evidence=provider_evidence,
            synthesis_input=synthesis_input,
        )
        cache_key = financial_final_answer_cache_key(symbol=symbol, payload=payload_data)
        cached_answer = get_cached_financial_final_answer(cache_key)
        if cached_answer is not None:
            if isinstance(timing, dict):
                timing["financialFinalAnswerCacheHit"] = True
            return cached_answer
        if isinstance(timing, dict):
            timing["financialFinalAnswerCacheHit"] = False
        if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
            if not runtime_context.acquire_llm("financial-synthesis"):
                return None
        try:
            if runtime_context is None and isinstance(timing, dict):
                timing["llmCalls"] = int(timing.get("llmCalls") or 0) + 1
            payload = {
                "model": os.getenv(
                    "AGENT_FINANCIAL_SYNTHESIZER_MODEL",
                    os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
                ),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You write beginner-friendly Korean SEC financial statement analysis. "
                            "Use only the supplied formatted facts and rule-based signals. "
                            "Do not calculate new numbers, infer missing metrics, mention raw JSON fields, or add prices, PER, PBR, PSR, forecasts, news, or buy/sell recommendations. "
                            "Explain what each important metric means, then state cautious interpretation and next metrics to check. "
                            "Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload_data, ensure_ascii=False),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "financial_final_answer",
                        "strict": True,
                        "schema": final_answer_json_schema(),
                    }
                },
            }
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            timeout_seconds = float(
                os.getenv(
                    "AGENT_FINANCIAL_SYNTHESIZER_TIMEOUT_SECONDS",
                    os.getenv("AGENT_SYNTHESIZER_TIMEOUT_SECONDS", "0"),
                )
            )
            if timeout_seconds <= 0 and runtime_context is not None:
                timeout_seconds = max(0.001, float(getattr(runtime_context.policy, "synthesis_timeout_ms", 1700)) / 1000)
            if timeout_seconds <= 0:
                timeout_seconds = 2.0
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            answer = final_answer_from_openai_json(parse_openai_text_json(data))
            if answer is not None:
                set_cached_financial_final_answer(cache_key, answer)
            return answer
        except Exception:
            return None

    def synthesize_agent_answer(
        self,
        *,
        symbol: str,
        intent: str,
        finding: AgentFinding,
        timing: dict[str, Any] | None = None,
        runtime_context: Any | None = None,
    ) -> AgentAnswer:
        openai_answer = self._synthesize_agent_answer_with_openai(
            symbol=symbol,
            intent=intent,
            finding=finding,
            timing=timing,
            runtime_context=runtime_context,
        )
        if openai_answer is not None:
            return openai_answer
        if isinstance(timing, dict):
            timing["roleAnswerLlmUnavailable"] = int(timing.get("roleAnswerLlmUnavailable") or 0) + 1
        return sanitize_agent_answer(AgentAnswer(
            agentId=finding.agentId,
            role=finding.role,
            title=f"{finding.role} 답변",
            content=finding.summary,
            confidence=finding.confidence,
            citations=citations_from_evidence(finding.evidence),
        ))

    def _synthesize_agent_answer_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        finding: AgentFinding,
        timing: dict[str, Any] | None = None,
        runtime_context: Any | None = None,
    ) -> AgentAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
            if not runtime_context.acquire_llm(f"{finding.role}-answer"):
                return None
        try:
            if runtime_context is None and isinstance(timing, dict):
                timing["llmCalls"] = int(timing.get("llmCalls") or 0) + 1
            payload = {
                "model": os.getenv("AGENT_ROLE_ANSWER_MODEL", os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2"))),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "Write one Korean stock-analysis answer for a single role agent from its evidence only. "
                            "Do not merge with other agents. Do not invent facts, prices, news, relationships, or recommendations. "
                            "Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            sanitize_value({
                                "symbol": symbol,
                                "intent": intent,
                                "roleFinding": {
                                    "agentId": finding.agentId,
                                    "role": finding.role,
                                    "summary": finding.summary,
                                    "rationale": finding.rationale,
                                    "confidence": finding.confidence,
                                    "evidence": compact_evidence(finding.evidence),
                                },
                            }).value,
                            ensure_ascii=False,
                        ),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "role_agent_answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["title", "content"],
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
            timeout_seconds = float(os.getenv("AGENT_ROLE_ANSWER_TIMEOUT_SECONDS", "0"))
            if timeout_seconds <= 0 and runtime_context is not None:
                timeout_seconds = max(0.001, float(getattr(runtime_context.policy, "synthesis_timeout_ms", 1700)) / 1000)
            if timeout_seconds <= 0:
                timeout_seconds = 12.0
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            parsed = parse_openai_text_json(data)
            title = parsed.get("title")
            content = parsed.get("content")
            if not isinstance(title, str) or not isinstance(content, str):
                return None
            return sanitize_agent_answer(AgentAnswer(
                agentId=finding.agentId,
                role=finding.role,
                title=title,
                content=content,
                confidence=finding.confidence,
                citations=citations_from_evidence(finding.evidence),
            ))
        except Exception:
            return None


def is_ontology_route(route: IntentRoute) -> bool:
    return route.intentType == "ontology" or route.selectedRoles == ["ontology"]


def is_news_route(route: IntentRoute) -> bool:
    return route.intentType == "news" or route.selectedRoles == ["news"]


def is_financial_route(route: IntentRoute) -> bool:
    intent_type = str(route.intentType or "").lower()
    return "financial" in intent_type or route.selectedRoles == ["financial"] or "financial" in route.selectedRoles


def build_summary(symbol: str, route: IntentRoute, findings: list[AgentFinding], evidence: list[EvidenceItem]) -> str:
    if evidence:
        return f"{symbol} 분석에 사용할 외부 근거를 확인했습니다."
    if findings:
        return f"{symbol} 분석을 완료했지만 외부 근거는 아직 충분하지 않습니다."
    return f"{symbol} 요청을 처리할 에이전트 근거가 아직 충분하지 않습니다."


def compact_findings(findings: list[AgentFinding]) -> list[dict[str, Any]]:
    return [
        {
            "agentId": item.agentId,
            "role": item.role,
            "summary": sanitize_text(item.summary).value,
            "rationale": sanitize_text(item.rationale).value,
            "confidence": item.confidence,
            "tags": item.tags,
        }
        for item in findings[:12]
    ]


def compact_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    compacted = []
    for item in items[:20]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        title = display_title(item)
        summary = display_summary(item)
        compacted.append(sanitize_value({
            "provider": item.provider,
            "status": item.status,
            "title": title,
            "summary": summary,
            "observedAt": item.observedAt,
            "url": item.url,
            "raw": {
                key: raw.get(key)
                for key in [
                    "publishedAt",
                    "source",
                    "impactDirection",
                    "eventType",
                    "subjectRelevance",
                    "relevanceScore",
                    "relevanceScoreV2",
                    "relevanceReason",
                    "directSignals",
                    "importanceScore",
                    "originalTitle",
                    "originalSummary",
                    "localizedTitle",
                    "localizedSummary",
                    "themeName",
                    "themeCategory",
                    "controlledName",
                    "confidence",
                    "accession",
                    "sourceUrl",
                    "ticker",
                    "companyName",
                    "sector",
                    "type",
                    "relationType",
                ]
                if key in raw
            },
        }).value)
    return compacted


def synthesis_payload(
    *,
    symbol: str,
    intent: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None,
) -> dict[str, Any]:
    if synthesis_input is not None:
        return sanitize_value({
            "symbol": symbol,
            "intent": intent,
            "synthesisInput": synthesis_input.to_dict(),
        }).value
    return sanitize_value({
        "symbol": symbol,
        "intent": intent,
        "route": route.to_dict(),
        "findings": compact_findings(findings),
        "providerEvidence": compact_evidence(provider_evidence),
    }).value


def final_answer_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "bullets"],
                },
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "provider": {"type": "string"},
                        "title": {"type": "string"},
                        "url": {"type": ["string", "null"]},
                        "publishedAt": {"type": ["string", "null"]},
                    },
                    "required": ["provider", "title", "url", "publishedAt"],
                },
            },
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "summary", "sections", "citations", "limitations"],
    }


def final_answer_from_openai_json(data: dict[str, Any]) -> FinalAnswer | None:
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    summary = data.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        return None
    sections = [
        FinalAnswerSection(
            title=str(item.get("title") or "근거"),
            bullets=[str(bullet) for bullet in item.get("bullets", []) if isinstance(bullet, (str, int, float))],
        )
        for item in data.get("sections", [])
        if isinstance(item, dict)
    ]
    citations = [
        FinalAnswerCitation(
            provider=str(item.get("provider") or "provider"),
            title=str(item.get("title") or "Evidence"),
            url=item.get("url") if isinstance(item.get("url"), str) else None,
            publishedAt=item.get("publishedAt") if isinstance(item.get("publishedAt"), str) else None,
        )
        for item in data.get("citations", [])
        if isinstance(item, dict)
    ]
    limitations = [str(item) for item in data.get("limitations", []) if isinstance(item, (str, int, float))]
    return sanitize_final_answer(FinalAnswer(
        title=title,
        summary=summary,
        sections=sections,
        citations=[item for item in citations if item.url],
        limitations=limitations,
    ))


def sanitize_final_answer(answer: FinalAnswer) -> FinalAnswer:
    return FinalAnswer(
        title=clean_user_text(answer.title),
        summary=clean_user_text(answer.summary),
        sections=[
            FinalAnswerSection(
                title=clean_user_text(section.title),
                bullets=[clean_user_text(bullet) for bullet in section.bullets],
            )
            for section in answer.sections
        ],
        citations=[sanitize_final_answer_citation(citation) for citation in answer.citations if citation.url],
        limitations=[clean_user_text(item) for item in answer.limitations],
    )


def sanitize_final_answer_citation(citation: FinalAnswerCitation) -> FinalAnswerCitation:
    title_result = sanitize_text(citation.title)
    url_result = sanitize_url(citation.url)
    return FinalAnswerCitation(
        provider=citation.provider,
        title=title_result.value,
        url=url_result.value,
        publishedAt=citation.publishedAt,
    )


def sanitize_agent_answer(answer: AgentAnswer) -> AgentAnswer:
    return AgentAnswer(
        agentId=answer.agentId,
        role=answer.role,
        title=clean_user_text(answer.title),
        content=clean_user_text(answer.content),
        confidence=answer.confidence,
        citations=[sanitize_final_answer_citation(citation) for citation in answer.citations if citation.url],
        createdAt=answer.createdAt,
    )


def clean_user_text(value: str) -> str:
    text = str(value)
    replacements = {
        "providerEvidence": "근거",
        "provider evidence": "근거",
        "Provider evidence": "근거",
        "provider 근거": "근거",
        "evidence": "근거",
        "Evidence": "근거",
        "Agent findings": "역할별 분석",
        "agent findings": "역할별 분석",
        "route.selectedRoles": "선택된 분석 역할",
        "route": "분석 경로",
        "guardrail": "검증",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"증거\((근거|evidence)\)", "근거", text)
    text = re.sub(r"근거\((근거|evidence)\)", "근거", text)
    return sanitize_text(text).value


def build_news_final_answer(
    symbol: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    daily_summaries: list[dict[str, Any]] | None = None,
) -> FinalAnswer:
    news_items = [item for item in provider_evidence if item.provider == "news" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "news" and item.status == "no-data"]
    title = "뉴스를 가져왔습니다"
    daily_summaries = [item for item in daily_summaries or [] if isinstance(item, dict)]
    if daily_summaries:
        latest = daily_summaries[0]
        return FinalAnswer(
            title=title,
            summary=str(latest.get("summary") or f"{symbol} 일일 뉴스 요약을 가져왔습니다."),
            sections=[],
            citations=[],
            limitations=[],
        )
    if not news_items:
        summary = no_data[0].summary if no_data and no_data[0].summary else f"{symbol} 관련 저장 뉴스가 없습니다."
        return FinalAnswer(
            title=title,
            summary=summary,
            sections=[],
            citations=[],
            limitations=[],
        )

    major_items = sorted(
        news_items,
        key=lambda item: (
            raw_number(item, "importanceScore"),
            raw_number(item, "relevanceScore"),
            raw_text(item, "publishedAt", ""),
        ),
        reverse=True,
    )
    direct_items = [item for item in major_items if raw_text(item, "subjectRelevance", "") in {"primary", "secondary"}]
    mention_items = [item for item in major_items if raw_text(item, "subjectRelevance", "") == "mention"]
    headline_items = direct_items or mention_items or major_items
    sections = [
        FinalAnswerSection(
            title="핵심 뉴스",
            bullets=[f"{display_title(item)}: {display_summary(item)}" for item in headline_items[:3]],
        )
    ]
    if not direct_items and mention_items:
        sections = []
    return FinalAnswer(
        title=title,
        summary=f"{symbol} 관련 뉴스 {len(news_items)}건을 가져왔습니다.",
        sections=sections,
        citations=[],
        limitations=[],
    )


def build_ontology_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    ontology_items = [item for item in provider_evidence if item.provider == "ontology" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "ontology" and item.status == "no-data"]
    graphdb_unavailable = any(raw_text(item, "relationType", "") == "graphdb-unavailable" for item in no_data)
    no_direct = [item for item in no_data if raw_text(item, "relationType", "") == "no-direct-control"]
    themes = unique_values(
        item.raw.get("themeName")
        for item in ontology_items
        if isinstance(item.raw, dict) and item.raw.get("themeName")
    )
    controls = [
        item
        for item in ontology_items
        if raw_text(item, "relationType", "") in {"control", "theme-control"}
    ]
    title = f"{symbol} 기업 관계 분석"

    if graphdb_unavailable:
        return FinalAnswer(
            title=title,
            summary=f"{symbol} 기업 관계 분석을 완료하지 못했습니다.",
            sections=[],
            citations=[],
            limitations=[item.summary for item in no_data[:5]],
        )
    if not ontology_items:
        return FinalAnswer(
            title=title,
            summary=f"GraphDB에서 {symbol} 관련 기업 관계 근거를 확인하지 못했습니다.",
            sections=[
                FinalAnswerSection(
                    title="확인되지 않은 내용",
                    bullets=[item.summary for item in no_data[:5]],
                )
            ],
            citations=[],
            limitations=["GraphDB는 연결됐지만 현재 repository에서 요청 ticker의 관계 근거가 충분하지 않습니다."],
        )

    if controls:
        control_summary = " 직접 지배/자회사 관계 근거도 확인했습니다."
    elif no_direct:
        control_summary = " 직접 지배/자회사 관계 근거는 확인되지 않았습니다."
    else:
        control_summary = ""
    theme_summary = f"{symbol}는 {', '.join(themes[:3])} 테마에 속합니다." if themes else f"{symbol} 관련 온톨로지 관계 근거를 확인했습니다."

    sections = [
        FinalAnswerSection(
            title="확인된 관계",
            bullets=[item.summary for item in ontology_items[:5]],
        )
    ]
    if themes:
        sections.append(FinalAnswerSection(title="관련 테마", bullets=themes[:5]))
    if no_direct:
        sections.append(FinalAnswerSection(title="확인되지 않은 내용", bullets=[item.summary for item in no_direct[:3]]))

    limitations = [item.summary for item in no_data if raw_text(item, "relationType", "") not in {"no-direct-control"}]
    if not limitations:
        limitations = ["GraphDB repository에 적재된 관계 근거 기준으로만 분석했습니다."]
    return FinalAnswer(
        title=title,
        summary=f"GraphDB 기준으로 {theme_summary}{control_summary}",
        sections=sections,
        citations=citations_from_evidence(ontology_items),
        limitations=limitations,
    )


FINANCIAL_METRIC_LABELS = {
    "revenue": "매출",
    "operating_income": "영업이익",
    "net_income": "순이익",
    "eps": "EPS",
    "assets": "총자산",
    "liabilities": "총부채",
    "equity": "자기자본",
    "current_assets": "유동자산",
    "current_liabilities": "유동부채",
    "operating_cash_flow": "영업활동 현금흐름",
    "free_cash_flow": "잉여현금흐름",
    "shares_outstanding": "발행주식수",
    "current_ratio": "유동비율",
    "net_margin": "순이익률",
    "operating_margin": "영업이익률",
    "revenue_growth": "매출 성장률",
    "net_income_growth": "순이익 성장률",
    "operating_income_growth": "영업이익 성장률",
    "liabilities_to_assets": "총부채/총자산",
    "liabilities_to_equity": "총부채/자기자본",
    "total_debt": "이자성 부채",
    "total_debt_to_assets": "이자성 부채/총자산",
    "total_debt_to_equity": "이자성 부채/자기자본",
    "interest_coverage": "이자보상배율",
}
FINANCIAL_MONEY_METRICS = {
    "revenue",
    "operating_income",
    "net_income",
    "assets",
    "liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
    "operating_cash_flow",
    "free_cash_flow",
    "total_debt",
}
FINANCIAL_PERCENT_METRICS = {
    "current_ratio",
    "net_margin",
    "operating_margin",
    "revenue_growth",
    "net_income_growth",
    "operating_income_growth",
    "liabilities_to_assets",
    "liabilities_to_equity",
    "total_debt_to_assets",
    "total_debt_to_equity",
}
FINANCIAL_DISPLAY_METRICS = set(FINANCIAL_METRIC_LABELS)
FINANCIAL_METRIC_ORDER = {
    "revenue": 0,
    "operating_income": 1,
    "net_income": 2,
    "eps": 3,
    "assets": 4,
    "liabilities": 5,
    "equity": 6,
    "current_assets": 7,
    "current_liabilities": 8,
    "current_ratio": 9,
    "operating_cash_flow": 10,
    "free_cash_flow": 11,
    "shares_outstanding": 12,
    "net_margin": 13,
    "operating_margin": 14,
    "liabilities_to_assets": 15,
    "liabilities_to_equity": 16,
    "total_debt": 17,
    "total_debt_to_assets": 18,
    "total_debt_to_equity": 19,
}
FINANCIAL_QUALITY_MESSAGES = {
    "missing_source": "일부 지표는 SEC 원천 항목이 부족해 표시하지 않았습니다.",
    "equity_includes_nci": "자기자본 일부 값은 비지배지분 포함 기준일 수 있습니다.",
    "frame_as_reported": "Peer 비교 값은 SEC frames 원 보고 기준이라 정정 공시와 차이가 날 수 있습니다.",
    "frame_coverage_gap": "일부 회사는 fiscal calendar 차이로 같은 SEC frame에 포함되지 않을 수 있습니다.",
    "stale": "캐시가 최신 공시와 다를 수 있어 재수집이 필요합니다.",
}


def build_financial_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    financial_items = [item for item in provider_evidence if item.provider == "financial" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "financial" and item.status == "no-data"]
    title = f"{symbol} SEC 재무 분석"
    if not financial_items:
        return FinalAnswer(
            title=title,
            summary=no_data[0].summary if no_data else f"{symbol} SEC 재무 근거가 없습니다.",
            sections=[],
            citations=[],
            limitations=["사용자 요청 시점에는 SEC API를 호출하지 않고 사전 계산된 Redis/ClickHouse snapshot만 사용합니다."],
        )

    summary_items = [item for item in financial_items if "peer" not in str(item.title).lower()]
    peer_items = [item for item in financial_items if "peer" in str(item.title).lower()]
    sections = []
    if summary_items:
        summary_item = summary_items[0]
        sections.append(FinalAnswerSection(
            title="공시 기반 재무 요약",
            bullets=financial_metric_bullets(summary_item) or [summary_item.summary],
        ))
        interpretation = financial_interpretation_bullets(summary_item)
        if interpretation:
            sections.append(FinalAnswerSection(title="해석 포인트", bullets=interpretation[:5]))
        sections.append(FinalAnswerSection(title="다음 확인 포인트", bullets=financial_next_step_bullets()))
    if peer_items:
        sections.append(FinalAnswerSection(
            title="Peer 비교",
            bullets=financial_peer_bullets(peer_items[0]) or [peer_items[0].summary],
        ))
    limitations = [
        "SEC companyfacts/frames 기반 사전 계산 snapshot만 사용했습니다.",
        "PER, PBR, PSR, 예상 실적처럼 주가나 외부 컨센서스가 필요한 지표는 이번 범위에서 제외했습니다.",
    ]
    warnings = unique_financial_warnings(financial_items)
    if warnings:
        limitations.extend(warnings[:4])
    return FinalAnswer(
        title=title,
        summary=financial_summary_text(symbol, summary_items[0]) if summary_items else financial_items[0].summary,
        sections=sections,
        citations=citations_from_evidence(financial_items),
        limitations=limitations,
    )


def build_market_move_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    visible_findings = visible_role_findings(findings)
    available = [item for item in provider_evidence if item.status == "available"]
    no_data = [item for item in provider_evidence if item.status == "no-data"]
    warnings = verification_warnings(findings)
    sections = [
        FinalAnswerSection(
            title="주가 변동 원인",
            bullets=[finding.summary for finding in visible_findings[:4]] or ["현재 확인 가능한 역할별 근거가 충분하지 않습니다."],
        )
    ]
    if available:
        sections.append(FinalAnswerSection(
            title="핵심 근거",
            bullets=[f"{display_title(item)}: {display_summary(item)}" for item in available[:6]],
        ))
    if warnings:
        sections.append(FinalAnswerSection(title="반대 근거 또는 불일치", bullets=warnings[:3]))
    limitations = [*warnings, *[item.summary for item in no_data[:6]]]
    if not limitations:
        limitations = ["차트, 뉴스, 거시, 기업 관계 provider에 현재 조회된 근거 기준으로 분석했습니다."]
    return FinalAnswer(
        title=f"{symbol} 주가 변동 원인 분석",
        summary=f"차트, 뉴스, 거시, 기업 관계 근거를 종합해 {symbol}의 변동 원인을 정리했습니다.",
        sections=sections,
        citations=citations_from_evidence(available),
        limitations=limitations,
    )


def build_general_final_answer(
    symbol: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
) -> FinalAnswer:
    available = [item for item in provider_evidence if item.status == "available"]
    no_data = [item for item in provider_evidence if item.status == "no-data"]
    visible_findings = visible_role_findings(findings)
    sections = []
    if visible_findings:
        sections.append(FinalAnswerSection(
            title="분석 요약",
            bullets=[finding.summary for finding in visible_findings[:4]],
        ))
    if available:
        sections.append(FinalAnswerSection(
            title="확인된 근거",
            bullets=[f"{display_title(item)}: {display_summary(item)}" for item in available[:5]],
        ))
    limitations = [item.summary for item in no_data[:5]]
    if not available and not limitations:
        limitations.append("외부 데이터 근거가 아직 충분하지 않습니다.")
    return FinalAnswer(
        title=f"{symbol} {route.intentType} 분석",
        summary=build_summary(symbol, route, visible_findings, available),
        sections=sections,
        citations=citations_from_evidence(available),
        limitations=limitations,
    )


def visible_role_findings(findings: list[AgentFinding]) -> list[AgentFinding]:
    return [
        item
        for item in findings
        if item.role in {"chart-analysis", "news-analysis", "macro-analysis", "company-relationship-analysis", "financial-analysis"}
    ]


def financial_metric_bullets(item: EvidenceItem) -> list[str]:
    bullets = []
    for metric in financial_display_metrics(item)[:12]:
        period = f" ({metric['period']})" if metric.get("period") else ""
        bullets.append(f"{metric['label']}: {metric['displayValue']}{period}")
    return bullets


def financial_peer_bullets(item: EvidenceItem) -> list[str]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    bullets = []
    for frame in financial_peer_frames(raw)[:6]:
        concept = financial_concept_label(frame.get("concept"))
        period = str(frame.get("display_period") or frame.get("displayPeriod") or frame.get("frame_period") or frame.get("framePeriod") or raw.get("frame_period") or "").strip()
        parts = []
        for peer in financial_frame_peers(frame)[:5]:
            symbol = str(peer.get("symbol") or "").upper() or "UNKNOWN"
            display = format_financial_value(str(peer.get("metric") or peer.get("concept") or frame.get("concept") or ""), peer.get("value"))
            if display:
                parts.append(f"{symbol} {display}")
        if not parts:
            continue
        suffix = f" ({period})" if period else ""
        bullets.append(f"{concept}: {', '.join(parts)}{suffix}")
    return bullets


def financial_peer_frames(raw: dict[str, Any]) -> list[dict[str, Any]]:
    frames = [frame for frame in raw.get("frames") or [] if isinstance(frame, dict)]
    comparable = [frame for frame in frames if len(financial_frame_peers(frame)) >= 2]
    if comparable:
        return comparable
    if frames:
        return frames
    peers = [peer for peer in raw.get("peers") or [] if isinstance(peer, dict)]
    if not peers:
        return []
    return [{
        "frame_period": raw.get("frame_period"),
        "concept": raw.get("concept") or (peers[0].get("concept") if peers else None),
        "unit": raw.get("unit") or (peers[0].get("unit") if peers else None),
        "peers": peers,
    }]


def financial_frame_peers(frame: dict[str, Any]) -> list[dict[str, Any]]:
    return [peer for peer in frame.get("peers") or [] if isinstance(peer, dict)]


def unique_financial_warnings(items: list[EvidenceItem]) -> list[str]:
    values = []
    for item in items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        for key in ("quality", "warning"):
            value = raw.get(key)
            message = friendly_financial_quality(value)
            if message and message not in values:
                values.append(message)
        for metric in raw.get("metrics") or []:
            if isinstance(metric, dict):
                message = friendly_financial_quality(metric.get("quality"))
                if message and message not in values:
                    values.append(message)
    return values


def financial_summary_text(symbol: str, item: EvidenceItem) -> str:
    metrics = {metric["metric"]: metric for metric in financial_display_metrics(item)}
    period = financial_period_from_item(item)
    prefix = f"{symbol}은 {period} SEC 공시 기준으로" if period else f"{symbol}은 SEC 공시 기준으로"
    assets = metrics.get("assets")
    liabilities = metrics.get("liabilities")
    equity = metrics.get("equity")
    if assets and liabilities and equity:
        return (
            f"{prefix} 총자산 {assets['displayValue']}, 총부채 {liabilities['displayValue']}, "
            f"자기자본 {equity['displayValue']}를 보유하고 있습니다."
        )
    revenue = metrics.get("revenue")
    net_income = metrics.get("net_income")
    if revenue and net_income:
        return f"{prefix} 매출 {revenue['displayValue']}, 순이익 {net_income['displayValue']}를 기록했습니다."
    if metrics:
        first = next(iter(metrics.values()))
        return f"{prefix} {first['label']} {first['displayValue']} 등 주요 재무 지표를 확인했습니다."
    return item.summary


def financial_display_metrics(item: EvidenceItem) -> list[dict[str, Any]]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    metrics = [metric for metric in raw.get("metrics") or [] if isinstance(metric, dict)]
    display_metrics = []
    for metric in sorted(metrics, key=financial_metric_sort_key):
        name = str(metric.get("metric") or "").strip()
        if not name:
            continue
        if name not in FINANCIAL_DISPLAY_METRICS:
            continue
        quality = str(metric.get("quality") or raw.get("quality") or "available")
        value = metric.get("value")
        if value is None or quality == "missing_source":
            continue
        numeric = as_float(value)
        display_value = format_financial_value(name, value)
        if not display_value:
            continue
        display_metrics.append({
            "metric": name,
            "label": FINANCIAL_METRIC_LABELS.get(name, name),
            "value": numeric if numeric is not None else value,
            "displayValue": display_value,
            "period": financial_metric_period(metric) or str(raw.get("latest_period") or "").strip(),
            "quality": quality,
            "selectedConcept": metric.get("selectedConcept") or metric.get("selected_concept") or metric.get("concept"),
        })
    return display_metrics


def financial_metric_sort_key(metric: dict[str, Any]) -> tuple[int, str, str]:
    name = str(metric.get("metric") or "")
    return (FINANCIAL_METRIC_ORDER.get(name, 100), str(metric.get("kind") or ""), name)


def financial_period_from_item(item: EvidenceItem) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    latest = str(raw.get("latest_period") or "").strip()
    if latest:
        return latest
    for metric in raw.get("metrics") or []:
        if isinstance(metric, dict):
            period = financial_metric_period(metric)
            if period:
                return period
    return ""


def financial_metric_period(metric: dict[str, Any]) -> str:
    fiscal_year = metric.get("fiscalYear") or metric.get("fiscal_year")
    fiscal_period = metric.get("fiscalPeriod") or metric.get("fiscal_period")
    parts = [str(part) for part in (fiscal_year, fiscal_period) if part not in (None, "")]
    if not parts:
        return ""
    if len(parts) == 2 and str(parts[0]).isdigit():
        return f"{parts[0]}년 {parts[1]}"
    return " ".join(parts)


def financial_interpretation_bullets(item: EvidenceItem) -> list[str]:
    return [f"{signal['label']}: {signal['explanation']}" for signal in financial_interpretation_signals(item)]


def financial_interpretation_signals(item: EvidenceItem) -> list[dict[str, str]]:
    metrics = {metric["metric"]: metric for metric in financial_display_metrics(item)}
    signals: list[dict[str, str]] = []
    current_ratio = metric_float(metrics, "current_ratio")
    if current_ratio is not None:
        if current_ratio < 1.0:
            signals.append(financial_signal("current_ratio_below_1", "caution", "단기 유동성 확인 필요", "유동비율이 100%보다 낮아 단기 자산만으로 단기 부채를 모두 덮기에는 여유가 크지 않은 편입니다."))
        elif current_ratio < 1.5:
            signals.append(financial_signal("current_ratio_moderate", "watch", "단기 유동성 보통", "유동비율이 100%는 넘지만 아주 넉넉한 수준은 아니어서 현금흐름과 함께 보는 것이 좋습니다."))
        else:
            signals.append(financial_signal("current_ratio_healthy", "positive", "단기 유동성 양호", "유동비율이 150% 이상이면 단기 지급 여력은 비교적 여유 있는 편으로 볼 수 있습니다."))
    liabilities_to_assets = metric_float(metrics, "liabilities_to_assets")
    if liabilities_to_assets is not None:
        if liabilities_to_assets >= 0.8:
            signals.append(financial_signal("liabilities_to_assets_high", "caution", "총부채 비중 높음", "총부채가 총자산의 80% 이상이면 부채 부담이 큰 구조일 수 있어 이자비용과 현금흐름을 함께 확인해야 합니다."))
        elif liabilities_to_assets >= 0.5:
            signals.append(financial_signal("liabilities_to_assets_moderate", "watch", "부채 활용도 보통 이상", "총부채가 총자산의 절반 이상이라 부채를 어느 정도 활용하는 재무 구조입니다."))
        else:
            signals.append(financial_signal("liabilities_to_assets_low", "positive", "부채 부담 낮은 편", "총자산 대비 총부채 비중이 낮아 재무 안정성 측면에서는 비교적 부담이 적은 편입니다."))
    liabilities_to_equity = metric_float(metrics, "liabilities_to_equity")
    if liabilities_to_equity is not None:
        if liabilities_to_equity >= 2.0:
            signals.append(financial_signal("liabilities_to_equity_high", "caution", "자기자본 대비 부채 큼", "총부채가 자기자본의 2배 이상이면 레버리지 부담을 따로 점검할 필요가 있습니다."))
        elif liabilities_to_equity >= 1.0:
            signals.append(financial_signal("liabilities_to_equity_moderate", "watch", "자기자본 대비 부채 보통 이상", "총부채가 자기자본보다 커서 안정성 판단에는 이익과 현금흐름 추세가 중요합니다."))
    net_margin = metric_float(metrics, "net_margin")
    if net_margin is not None:
        if net_margin >= 0.2:
            signals.append(financial_signal("net_margin_strong", "positive", "수익성 강함", "순이익률이 20% 이상이면 매출을 이익으로 남기는 힘이 강한 편입니다."))
        elif net_margin < 0.05:
            signals.append(financial_signal("net_margin_thin", "caution", "순이익률 낮음", "순이익률이 낮으면 매출 변동이나 비용 증가에 이익이 민감하게 흔들릴 수 있습니다."))
    operating_margin = metric_float(metrics, "operating_margin")
    if operating_margin is not None:
        if operating_margin >= 0.2:
            signals.append(financial_signal("operating_margin_strong", "positive", "영업 수익성 강함", "영업이익률이 20% 이상이면 본업에서 이익을 남기는 힘이 강한 편입니다."))
        elif operating_margin < 0.05:
            signals.append(financial_signal("operating_margin_thin", "caution", "영업 수익성 낮음", "영업이익률이 낮으면 본업 수익성이 약하거나 비용 부담이 큰 상태일 수 있습니다."))
    free_cash_flow = metric_float(metrics, "free_cash_flow")
    if free_cash_flow is not None:
        if free_cash_flow > 0:
            signals.append(financial_signal("free_cash_flow_positive", "positive", "현금창출 양호", "잉여현금흐름이 플러스면 투자 지출 이후에도 회사에 남는 현금이 있다는 뜻입니다."))
        elif free_cash_flow < 0:
            signals.append(financial_signal("free_cash_flow_negative", "caution", "현금흐름 확인 필요", "잉여현금흐름이 마이너스면 투자 지출이나 영업 현금흐름 부담을 확인해야 합니다."))
    for metric_name, label in (
        ("revenue_growth", "매출 성장률"),
        ("net_income_growth", "순이익 성장률"),
        ("operating_income_growth", "영업이익 성장률"),
    ):
        growth = metric_float(metrics, metric_name)
        if growth is None:
            continue
        if growth > 0:
            signals.append(financial_signal(f"{metric_name}_positive", "positive", f"{label} 플러스", f"{label}이 플러스라 전년 동기 대비 성장 흐름이 확인됩니다."))
        elif growth < 0:
            signals.append(financial_signal(f"{metric_name}_negative", "watch", f"{label} 마이너스", f"{label}이 마이너스라 성장 둔화 원인을 확인해야 합니다."))
    if not signals:
        signals.append(financial_signal("limited_ratio_context", "info", "해석 지표 제한", "현재 snapshot에서는 비율과 성장률 정보가 충분하지 않아 숫자 자체의 의미 해석은 제한적입니다."))
    return signals[:7]


def financial_signal(signal: str, severity: str, label: str, explanation: str) -> dict[str, str]:
    return {
        "signal": signal,
        "severity": severity,
        "label": label,
        "explanation": explanation,
    }


def financial_next_step_bullets() -> list[str]:
    return [
        "매출 성장률과 영업이익률 추세를 함께 보면 본업 성장성과 수익성을 더 잘 판단할 수 있습니다.",
        "영업활동 현금흐름과 잉여현금흐름을 보면 회계상 이익이 실제 현금으로 이어지는지 확인할 수 있습니다.",
        "PER, PBR, PSR 같은 주가 기반 밸류에이션은 SEC 재무제표만으로는 부족하고 별도 시장 가격 데이터가 필요합니다.",
    ]


def metric_float(metrics: dict[str, dict[str, Any]], name: str) -> float | None:
    metric = metrics.get(name)
    if not metric:
        return None
    return as_float(metric.get("value"))


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def format_financial_value(metric: str, value: Any) -> str:
    number = as_float(value)
    if number is None:
        text = str(value or "").strip()
        return text
    metric_key = str(metric or "").strip()
    concept_key = metric_key.lower()
    if metric_key in FINANCIAL_PERCENT_METRICS:
        return f"{format_number(number * 100, 1)}%"
    if metric_key in FINANCIAL_MONEY_METRICS or any(term in concept_key for term in ("revenue", "income", "assets", "liabilities", "cash", "debt")):
        return format_usd_amount(number)
    if metric_key == "eps" or "earningspershare" in concept_key:
        return f"약 {format_number(number, 2)}달러"
    if metric_key == "shares_outstanding" or "shares" in concept_key:
        return format_shares(number)
    if metric_key == "interest_coverage":
        return f"{format_number(number, 1)}배"
    if abs(number) >= 1_000_000:
        return format_usd_amount(number)
    return format_number(number, 2)


def format_usd_amount(value: float) -> str:
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1_000_000_000_000:
        return f"{sign}약 {format_number(amount / 1_000_000_000_000, 2)}조 달러"
    if amount >= 100_000_000:
        return f"{sign}약 {format_number(amount / 100_000_000, 0)}억 달러"
    if amount >= 10_000:
        return f"{sign}약 {format_number(amount / 10_000, 0)}만 달러"
    return f"{sign}약 {format_number(amount, 0)}달러"


def format_shares(value: float) -> str:
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 100_000_000:
        return f"{sign}약 {format_number(amount / 100_000_000, 1)}억 주"
    if amount >= 10_000:
        return f"{sign}약 {format_number(amount / 10_000, 0)}만 주"
    return f"{sign}약 {format_number(amount, 0)}주"


def format_number(value: float, digits: int) -> str:
    text = f"{value:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def friendly_financial_quality(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text == "available":
        return None
    return FINANCIAL_QUALITY_MESSAGES.get(text, text.replace("_", " "))


def financial_concept_label(value: Any) -> str:
    text = str(value or "").strip()
    normalized = text.lower()
    if "revenue" in normalized or text == "Revenues":
        return "매출"
    if "netincome" in normalized or "net income" in normalized:
        return "순이익"
    if "operatingincome" in normalized or "operating income" in normalized:
        return "영업이익"
    if normalized == "assets" or normalized == "assetscurrent":
        return "총자산" if normalized == "assets" else "유동자산"
    if normalized == "liabilities" or normalized == "liabilitiescurrent":
        return "총부채" if normalized == "liabilities" else "유동부채"
    if "stockholdersequity" in normalized or "stockholders equity" in normalized:
        return "자기자본"
    if "sharesoutstanding" in normalized or "shares outstanding" in normalized:
        return "발행주식수"
    return FINANCIAL_METRIC_LABELS.get(text, text or "지표")


def financial_synthesis_payload(
    *,
    symbol: str,
    intent: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None,
) -> dict[str, Any]:
    financial_items = [item for item in provider_evidence if item.provider == "financial" and item.status == "available"]
    summary_item = next((item for item in financial_items if "peer" not in str(item.title).lower()), None)
    peer_item = next((item for item in financial_items if "peer" in str(item.title).lower()), None)
    facts = financial_display_metrics(summary_item) if summary_item else []
    signals = financial_interpretation_signals(summary_item) if summary_item else []
    limitations = [
        "SEC 공시 기반 사전 계산 데이터만 사용했습니다.",
        "PER, PBR, PSR, 예상 실적처럼 주가나 외부 컨센서스가 필요한 지표는 제외했습니다.",
        *unique_financial_warnings(financial_items),
    ]
    payload = {
        "symbol": symbol,
        "intent": intent,
        "route": route.to_dict(),
        "period": financial_period_from_item(summary_item) if summary_item else "",
        "facts": facts[:16],
        "signals": signals[:8],
        "peerComparisons": financial_peer_payload(peer_item) if peer_item else [],
        "limitations": unique_strings(limitations)[:8],
        "answerPolicy": [
            "제공된 facts와 signals만 사용합니다.",
            "숫자를 새로 계산하지 않습니다.",
            "매수/매도/목표가 같은 투자 행동 조언을 하지 않습니다.",
            "초보 투자자가 이해할 수 있게 지표 의미와 다음 확인 포인트를 설명합니다.",
        ],
        "fallbackSummary": financial_summary_text(symbol, summary_item) if summary_item else "",
        "findingSummaries": [finding.summary for finding in findings if finding.role == "financial-analysis"][:3],
    }
    if synthesis_input is not None:
        payload["snapshotIntent"] = synthesis_input.intent
    return sanitize_value(payload).value


def financial_peer_payload(item: EvidenceItem | None) -> list[dict[str, Any]]:
    if item is None:
        return []
    raw = item.raw if isinstance(item.raw, dict) else {}
    peers = []
    for frame in financial_peer_frames(raw):
        frame_period = str(frame.get("display_period") or frame.get("displayPeriod") or frame.get("frame_period") or frame.get("framePeriod") or raw.get("frame_period") or "").strip()
        for peer in financial_frame_peers(frame):
            value = peer.get("value")
            display = format_financial_value(str(peer.get("metric") or peer.get("concept") or frame.get("concept") or ""), value)
            if not display:
                continue
            peers.append({
                "symbol": str(peer.get("symbol") or "").upper(),
                "metric": financial_concept_label(peer.get("concept") or peer.get("metric") or frame.get("concept")),
                "displayValue": display,
                "framePeriod": frame_period,
                "quality": friendly_financial_quality(peer.get("quality")),
            })
    return peers[:10]


def financial_final_answer_cache_key(*, symbol: str, payload: dict[str, Any]) -> str:
    normalized_symbol = str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:24]
    prefix = os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_CACHE_PREFIX", "gops:agent:financial-final-answer:v1")
    return f"{prefix}:{normalized_symbol}:{digest}"


def financial_final_answer_cache_enabled() -> bool:
    return str(os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_CACHE_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def get_cached_financial_final_answer(key: str) -> FinalAnswer | None:
    if not financial_final_answer_cache_enabled():
        return None
    client = financial_final_answer_cache_client()
    if client is None:
        return None
    try:
        payload = client.get(key)
        if not payload:
            return None
        decoded = json.loads(payload)
        answer = final_answer_from_dict(decoded.get("finalAnswer") if isinstance(decoded, dict) else decoded)
        return sanitize_final_answer(answer) if answer else None
    except Exception:
        return None


def set_cached_financial_final_answer(key: str, answer: FinalAnswer) -> None:
    if not financial_final_answer_cache_enabled():
        return
    ttl_seconds = int(os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_CACHE_TTL_SECONDS", "86400"))
    if ttl_seconds <= 0:
        return
    client = financial_final_answer_cache_client()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps({"finalAnswer": answer.to_dict()}, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return


def financial_final_answer_cache_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis

        return redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.2")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0.2")),
            health_check_interval=int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "30")),
        )
    except Exception:
        return None


def unique_strings(values: list[str]) -> list[str]:
    unique = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return unique


def verification_warnings(findings: list[AgentFinding]) -> list[str]:
    warnings = []
    for finding in findings:
        if finding.role != "verification-guardrail":
            continue
        normalized = finding.summary.strip().lower()
        if normalized and not normalized.startswith("no trading-action guardrail violation detected"):
            warnings.append(finding.summary)
    return warnings


def citations_from_evidence(items: list[EvidenceItem]) -> list[FinalAnswerCitation]:
    return [
        FinalAnswerCitation(
            provider=item.provider,
            title=display_title(item),
            url=item.url,
            publishedAt=item.raw.get("publishedAt") if isinstance(item.raw, dict) else None,
        )
        for item in items[:8]
        if item.url
    ]


def raw_text(item: EvidenceItem, key: str, fallback: str) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return str(value) if value else fallback


def display_title(item: EvidenceItem) -> str:
    if item.provider == "news":
        return raw_text(item, "localizedTitle", item.title)
    return item.title


def display_summary(item: EvidenceItem) -> str:
    if item.provider == "news":
        return raw_text(item, "localizedSummary", item.summary)
    return item.summary


def raw_number(item: EvidenceItem, key: str) -> float:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def dominant_label(counter: Counter, fallback: str) -> str:
    if not counter:
        return fallback
    for label, _count in counter.most_common():
        if label != "unknown":
            return label
    return fallback


def unique_values(values) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def impact_direction_label(value: str) -> str:
    labels = {
        "positive": "긍정",
        "negative": "부정",
        "mixed": "혼재",
        "unknown": "판단 보류",
    }
    return labels.get(value, value)
