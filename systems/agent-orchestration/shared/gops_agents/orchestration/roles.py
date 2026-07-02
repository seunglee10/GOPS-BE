from __future__ import annotations

from typing import Any

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


def resolve_requested_roles(agent_ids: Any) -> set[str]:
    all_roles = {"chart", "news", "macro", "ontology"}
    if not isinstance(agent_ids, list) or not agent_ids:
        return all_roles

    id_to_role = {
        "agent-01": "chart",
        "agent-02": "news",
        "agent-03": "macro",
        "agent-04": "ontology",
        "chart-agent": "chart",
        "news-agent": "news",
        "macro-agent": "macro",
        "ontology-agent": "ontology",
    }
    roles = {
        role
        for item in agent_ids
        if isinstance(item, str)
        for role in [id_to_role.get(item)]
        if role
    }
    return roles or all_roles
