from __future__ import annotations

import json
import os
import sys
from typing import Any

from gops_agents.orchestrator import AgentOrchestrator


DEFAULT_CASES = [
    {"symbol": "NVDA", "intent": "NVDA 뉴스 근거로 상승/하락 요인을 설명해줘", "agentIds": ["agent-02", "agent-04"]},
    {"symbol": "MSFT", "intent": "MSFT 시장 요약을 근거와 함께 정리해줘", "agentIds": ["agent-01", "agent-02"]},
]


def main() -> int:
    cases = case_set_from_env()
    orchestrator = AgentOrchestrator()
    rows = []
    for case in cases:
        report = orchestrator.analyze(query_payload(case))
        rows.append(score_report(report.to_dict()))
    result = {
        "status": "ok",
        "count": len(rows),
        "averageCitationGrounding": average([row["citationGrounding"] for row in rows]),
        "averageEvidenceMentionRate": average([row["evidenceMentionRate"] for row in rows]),
        "rows": rows,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def case_set_from_env() -> list[dict[str, Any]]:
    raw = os.getenv("AGENT_GROUNDING_EVAL_CASES_JSON")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except Exception:
            pass
    return list(DEFAULT_CASES)


def query_payload(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": case.get("symbol"),
        "intent": case.get("intent") or "analysis",
        "agentIds": case.get("agentIds") or ["agent-02"],
    }


def score_report(report: dict[str, Any]) -> dict[str, Any]:
    evidence = [item for item in report.get("providerEvidence", []) if isinstance(item, dict) and item.get("status") == "available"]
    final_answer = dict(report.get("finalAnswer") or {})
    citations = [item for item in final_answer.get("citations", []) if isinstance(item, dict)]
    answer_text = final_answer_text(final_answer)
    evidence_keys = evidence_reference_keys(evidence)
    citation_hits = count_grounded_citations(citations, evidence)
    mentioned_evidence = sum(1 for key in evidence_keys if key and key.lower() in answer_text.lower())
    citation_grounding = citation_hits / len(citations) if citations else (1.0 if not evidence else 0.0)
    evidence_mention_rate = mentioned_evidence / len(evidence_keys) if evidence_keys else 1.0
    return {
        "analysisId": report.get("analysisId"),
        "symbol": report.get("symbol"),
        "status": report.get("status"),
        "availableEvidence": len(evidence),
        "citations": len(citations),
        "groundedCitations": citation_hits,
        "citationGrounding": round(float(citation_grounding), 3),
        "evidenceMentionRate": round(float(evidence_mention_rate), 3),
        "totalMs": float((report.get("timing") or {}).get("totalMs") or 0.0),
    }


def final_answer_text(final_answer: dict[str, Any]) -> str:
    parts = [str(final_answer.get("title") or ""), str(final_answer.get("summary") or "")]
    for section in final_answer.get("sections", []):
        if not isinstance(section, dict):
            continue
        parts.append(str(section.get("title") or ""))
        parts.extend(str(item) for item in section.get("bullets", []) if isinstance(item, (str, int, float)))
    return "\n".join(parts)


def evidence_reference_keys(evidence: list[dict[str, Any]]) -> list[str]:
    keys = []
    for item in evidence:
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        for value in [raw.get("articleId"), item.get("title"), item.get("url")]:
            text = str(value or "").strip()
            if text:
                keys.append(text)
                break
    return keys


def count_grounded_citations(citations: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> int:
    hits = 0
    evidence_titles = {str(item.get("title") or "").strip().lower() for item in evidence if item.get("title")}
    evidence_urls = {str(item.get("url") or "").strip() for item in evidence if item.get("url")}
    for citation in citations:
        title = str(citation.get("title") or "").strip().lower()
        url = str(citation.get("url") or "").strip()
        if (title and title in evidence_titles) or (url and url in evidence_urls):
            hits += 1
    return hits


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(float(item) for item in values) / len(values), 3)


if __name__ == "__main__":
    sys.exit(main())
