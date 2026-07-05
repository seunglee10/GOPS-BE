from __future__ import annotations

import json
import os
import re
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
        sections.append(FinalAnswerSection(
            title="공시 기반 재무 요약",
            bullets=financial_metric_bullets(summary_items[0]) or [summary_items[0].summary],
        ))
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
        summary=summary_items[0].summary if summary_items else financial_items[0].summary,
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
    raw = item.raw if isinstance(item.raw, dict) else {}
    metrics = [metric for metric in raw.get("metrics") or [] if isinstance(metric, dict)]
    order = {
        "revenue": 0,
        "operating_income": 1,
        "net_income": 2,
        "eps": 3,
        "assets": 4,
        "liabilities": 5,
        "equity": 6,
        "operating_cash_flow": 7,
        "free_cash_flow": 8,
        "shares_outstanding": 9,
    }
    metrics = sorted(metrics, key=lambda metric: (order.get(str(metric.get("metric") or ""), 100), str(metric.get("kind") or ""), str(metric.get("metric") or "")))
    bullets = []
    for metric in metrics[:10]:
        name = metric.get("metric")
        value = metric.get("value")
        period = " ".join(str(part) for part in (metric.get("fiscalYear"), metric.get("fiscalPeriod")) if part)
        quality = metric.get("quality")
        suffix = f" ({quality})" if quality and quality != "available" else ""
        bullets.append(f"{name}: {value} {period}".strip() + suffix)
    return bullets


def financial_peer_bullets(item: EvidenceItem) -> list[str]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    frame_period = str(raw.get("frame_period") or "").strip()
    peers = [peer for peer in raw.get("peers") or [] if isinstance(peer, dict)]
    bullets = []
    for peer in peers[:6]:
        quality = peer.get("quality")
        suffix = f" ({quality})" if quality and quality != "available" else ""
        bullets.append(f"{peer.get('symbol')}: {peer.get('concept')}={peer.get('value')} {frame_period}".strip() + suffix)
    return bullets


def unique_financial_warnings(items: list[EvidenceItem]) -> list[str]:
    values = []
    for item in items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        for key in ("quality", "warning"):
            value = raw.get(key)
            if value and value != "available" and str(value) not in values:
                values.append(str(value))
        for metric in raw.get("metrics") or []:
            if isinstance(metric, dict):
                quality = metric.get("quality")
                if quality and quality != "available" and str(quality) not in values:
                    values.append(str(quality))
    return values


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
