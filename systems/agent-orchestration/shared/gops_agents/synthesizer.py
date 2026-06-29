from __future__ import annotations

from .contracts import (
    AgentFinding,
    EvidenceItem,
    FinalAnswer,
    FinalAnswerCitation,
    FinalAnswerSection,
    IntentRoute,
)


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


def build_summary(symbol: str, route: IntentRoute, findings: list[AgentFinding], evidence: list[EvidenceItem]) -> str:
    if evidence:
        return f"{symbol} 요청은 {', '.join(route.selectedRoles)} 역할로 라우팅했고, 저장된 provider 근거 {len(evidence)}건을 확인했습니다."
    if findings:
        return f"{symbol} 요청은 {', '.join(route.selectedRoles)} 역할로 라우팅했지만, 외부 provider 근거는 아직 충분하지 않습니다."
    return f"{symbol} 요청을 처리할 에이전트 근거가 아직 충분하지 않습니다."
