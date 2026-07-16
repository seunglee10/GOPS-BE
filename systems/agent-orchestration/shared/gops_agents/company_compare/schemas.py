from __future__ import annotations

from typing import Any


SECTION_IDS = (
    "business_model",
    "growth_style",
    "profit_structure",
    "financial_health",
    "earnings_stability",
    "risk_profile",
    "relationship",
    "recent_flow",
)


def compare_section_schema(
    *,
    section_ids: tuple[str, ...] = SECTION_IDS,
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """M2부터 사용하는 판정 없는 서술 섹션 스키마."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "heading", "analysis", "evidenceRefs"],
        "properties": {
            "id": {"type": "string", "enum": list(section_ids)},
            "heading": {"type": "string", "minLength": 1},
            "analysis": {"type": "string", "minLength": 1},
            "evidenceRefs": {
                "type": "array",
                "items": (
                    {"type": "string", "enum": list(evidence_refs)}
                    if evidence_refs
                    else {"type": "string"}
                ),
                "minItems": 1,
                "maxItems": 20,
            },
        },
    }


def company_compare_schema(
    *,
    section_ids: tuple[str, ...] = SECTION_IDS,
    evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Compare Agent 서술 레이어의 strict structured-output 계약.

    정량 지표는 이 스키마에 넣지 않는다. 서버가 계산한 quantitative payload와
    LLM이 작성할 narrative payload를 분리해 수치 재계산과 우열 판정을 막는다.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "sections", "insights", "dataGaps"],
        "properties": {
            "summary": {"type": "string", "minLength": 1},
            "sections": {
                "type": "array",
                "minItems": len(section_ids),
                "maxItems": len(section_ids),
                "items": compare_section_schema(section_ids=section_ids, evidence_refs=evidence_refs),
            },
            "insights": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
            },
            "dataGaps": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
        },
    }
