from __future__ import annotations

from ..contracts import AgentFinding, EvidenceItem


def role_agent_error_finding(role: str, symbol: str, exc: Exception):
    role_to_provider = {
        "news": "news",
        "macro": "macro",
        "ontology": "ontology",
        "chart": "chart",
    }
    provider = role_to_provider.get(role, role)
    return AgentFinding(
        agentId=f"{role}-agent",
        role={
            "chart": "chart-analysis",
            "news": "news-analysis",
            "macro": "macro-analysis",
            "ontology": "company-relationship-analysis",
        }.get(role, role),
        summary=f"{symbol} {role} 분석 중 오류가 발생했습니다.",
        rationale=f"{exc.__class__.__name__}: role agent execution failed.",
        confidence=0.1,
        evidence=[
            EvidenceItem(
                provider=provider,
                status="no-data",
                title=f"{role} agent unavailable",
                summary=f"{role} agent 실행 중 오류가 발생했습니다: {exc.__class__.__name__}",
            )
        ],
        tags=[role, "agent-error"],
    )
