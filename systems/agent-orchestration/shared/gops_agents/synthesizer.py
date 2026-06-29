from __future__ import annotations

import json
import os
import urllib.request
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
        available = [item for item in provider_evidence if item.status == "available"]
        no_data = [item for item in provider_evidence if item.status == "no-data"]
        visible_findings = [item for item in findings if item.role in {
            "chart-analysis",
            "news-analysis",
            "macro-analysis",
            "company-relationship-analysis",
        }]
        title = f"{symbol} {route.intentType} 분석"
        summary = build_summary(symbol, route, visible_findings, available)
        sections = []
        if available:
            sections.append(FinalAnswerSection(
                title="확인된 근거",
                bullets=[f"{item.title}: {item.summary}" for item in available[:5]],
            ))
        if visible_findings:
            sections.append(FinalAnswerSection(
                title="에이전트 판단",
                bullets=[finding.summary for finding in visible_findings[:4]],
            ))
        limitations = [f"{item.title}: {item.summary}" for item in no_data[:5]]
        if not available and not limitations:
            limitations.append("외부 provider 근거가 아직 충분하지 않습니다.")
        citations = [
            FinalAnswerCitation(
                provider=item.provider,
                title=item.title,
                url=item.url,
                publishedAt=item.raw.get("publishedAt") if isinstance(item.raw, dict) else None,
            )
            for item in available[:5]
        ]
        return FinalAnswer(
            title=title,
            summary=summary,
            sections=sections,
            citations=citations,
            limitations=limitations,
        )

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
        return f"{symbol} 요청은 {', '.join(route.selectedRoles)} 역할로 라우팅했고, 저장된 provider 근거 {len(evidence)}건을 확인했습니다."
    if findings:
        return f"{symbol} 요청은 {', '.join(route.selectedRoles)} 역할로 라우팅했지만, 외부 provider 근거는 아직 충분하지 않습니다."
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
    return FinalAnswer(
        title=title,
        summary=summary,
        sections=sections,
        citations=citations,
        limitations=limitations,
    )
