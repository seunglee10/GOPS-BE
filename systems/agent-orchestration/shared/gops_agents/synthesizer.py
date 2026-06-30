from __future__ import annotations

import json
import os
import re
import urllib.request
from collections import Counter
from typing import Any

from .contracts import (
    AgentFinding,
    EvidenceItem,
    FinalAnswer,
    FinalAnswerCitation,
    FinalAnswerSection,
    IntentRoute,
)
from .router import parse_openai_text_json


class FinalAnswerSynthesizer:
    def synthesize(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
    ) -> FinalAnswer:
        openai_answer = self._synthesize_with_openai(
            symbol=symbol,
            intent=intent,
            route=route,
            findings=findings,
            provider_evidence=provider_evidence,
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
    ) -> FinalAnswer:
        if route.intentType == "news" or route.selectedRoles == ["news"]:
            return build_news_final_answer(symbol, findings, provider_evidence)
        if route.intentType == "ontology" or route.selectedRoles == ["ontology"]:
            return build_ontology_final_answer(symbol, findings, provider_evidence)
        if route.intentType == "market-move":
            return build_market_move_final_answer(symbol, findings, provider_evidence)
        return build_general_final_answer(symbol, route, findings, provider_evidence)

    def _synthesize_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
    ) -> FinalAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key or os.getenv("AGENT_FINAL_ANSWER_PROVIDER") == "deterministic":
            return None
        try:
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
                            {
                                "symbol": symbol,
                                "intent": intent,
                                "route": route.to_dict(),
                                "findings": compact_findings(findings),
                                "providerEvidence": compact_evidence(provider_evidence),
                            },
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
            with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_SYNTHESIZER_TIMEOUT_SECONDS", "12"))) as response:
                data = json.loads(response.read().decode("utf-8"))
            return final_answer_from_openai_json(parse_openai_text_json(data))
        except Exception:
            return None


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
            "summary": item.summary,
            "rationale": item.rationale,
            "confidence": item.confidence,
            "tags": item.tags,
        }
        for item in findings[:12]
    ]


def compact_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    compacted = []
    for item in items[:20]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        compacted.append({
            "provider": item.provider,
            "status": item.status,
            "title": item.title,
            "summary": item.summary,
            "observedAt": item.observedAt,
            "url": item.url,
            "raw": {
                key: raw.get(key)
                for key in [
                    "publishedAt",
                    "source",
                    "impactDirection",
                    "eventType",
                    "relevanceScore",
                    "importanceScore",
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
        })
    return compacted


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
        citations=[citation for citation in answer.citations if citation.url],
        limitations=[clean_user_text(item) for item in answer.limitations],
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
    return text


def build_news_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    news_items = [item for item in provider_evidence if item.provider == "news" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "news" and item.status == "no-data"]
    warnings = verification_warnings(findings)
    title = f"{symbol} 뉴스 분석"
    if not news_items:
        return FinalAnswer(
            title=title,
            summary=f"{symbol} 관련 저장 뉴스가 아직 충분하지 않습니다.",
            sections=[],
            citations=[],
            limitations=[item.summary for item in no_data[:5]] or ["뉴스 provider에서 반환된 기사 근거가 없습니다."],
        )

    directions = Counter(raw_text(item, "impactDirection", "unknown") for item in news_items)
    dominant_direction = dominant_label(directions, "unknown")
    major_items = sorted(
        news_items,
        key=lambda item: (
            raw_number(item, "importanceScore"),
            raw_number(item, "relevanceScore"),
            raw_text(item, "publishedAt", ""),
        ),
        reverse=True,
    )
    sections = [
        FinalAnswerSection(
            title="핵심 뉴스",
            bullets=[f"{item.title}: {item.summary}" for item in major_items[:5]],
        ),
        FinalAnswerSection(
            title="주가 영향 방향",
            bullets=[f"저장된 뉴스 키워드 기준 영향 방향은 {impact_direction_label(dominant_direction)}입니다."],
        ),
    ]
    limitations = ["뉴스 provider에 저장된 기사 기준이며, 실시간 전체 뉴스 범위는 보장하지 않습니다.", *warnings]
    return FinalAnswer(
        title=title,
        summary=f"최근 저장된 뉴스 기준으로 {symbol} 관련 주요 이슈를 정리했습니다.",
        sections=sections,
        citations=citations_from_evidence(news_items),
        limitations=limitations,
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
            bullets=[f"{item.title}: {item.summary}" for item in available[:6]],
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
            bullets=[f"{item.title}: {item.summary}" for item in available[:5]],
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
        if item.role in {"chart-analysis", "news-analysis", "macro-analysis", "company-relationship-analysis"}
    ]


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
            title=item.title,
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
