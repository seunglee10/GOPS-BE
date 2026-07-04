from __future__ import annotations

import json
import os
import sys
from typing import Any

from gops_agents.orchestrator import AgentOrchestrator


DEFAULT_CASES = [
    {
        "symbol": "NVDA",
        "intent": "NVDA 관련 뉴스 영향 분석",
        "agentIds": ["agent-02", "agent-04"],
        "expectedRelatedSymbols": ["AMD", "TSM"],
        "expectedIntentType": "news",
        "expectedEntitySymbol": "NVDA",
    },
    {
        "symbol": "AAPL",
        "intent": "AAPL 뉴스와 시장 영향을 요약해줘",
        "agentIds": ["agent-02"],
        "expectedRelatedSymbols": [],
        "expectedIntentType": "news",
        "expectedEntitySymbol": "AAPL",
    },
]


def main() -> int:
    cases = case_set_from_env()
    orchestrator = AgentOrchestrator()
    rows = []
    for case in cases:
        report = orchestrator.analyze(query_payload(case))
        rows.append(score_case(case, report.to_dict()))
    result = {
        "status": "ok",
        "count": len(rows),
        "averageRouteAccuracy": average([row["routeAccuracy"] for row in rows]),
        "averageEntityAccuracy": average([row["entityAccuracy"] for row in rows]),
        "averageRelationRecall": average([row["relationRecall"] for row in rows]),
        "averageEvidencePrecision": average([row["evidencePrecision"] for row in rows]),
        "rows": rows,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def case_set_from_env() -> list[dict[str, Any]]:
    raw = os.getenv("AGENT_RETRIEVAL_EVAL_CASES_JSON")
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


def score_case(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    timing = dict(report.get("timing") or {})
    retrieval = dict((report.get("agentTrace") or {}).get("retrievalContext") or {})
    expansion = dict(retrieval.get("graph_expansion") or {})
    related_symbols = [
        str(item.get("symbol") or "").upper()
        for item in expansion.get("related_symbols", [])
        if isinstance(item, dict) and item.get("symbol")
    ]
    expected = {str(item).upper() for item in case.get("expectedRelatedSymbols", []) if item}
    expected_intent = str(case.get("expectedIntentType") or "").strip()
    expected_entity = str(case.get("expectedEntitySymbol") or case.get("symbol") or "").upper()
    actual_intent = str((report.get("route") or {}).get("intentType") or "")
    actual_entities = [
        str(item.get("ticker") or item.get("canonical_name") or "").upper()
        for item in report.get("resolvedEntities", [])
        if isinstance(item, dict)
    ]
    available_evidence = [
        item for item in report.get("providerEvidence", [])
        if isinstance(item, dict) and item.get("status") == "available"
    ]
    all_evidence = [item for item in report.get("providerEvidence", []) if isinstance(item, dict)]
    hits = len(expected.intersection(related_symbols)) if expected else len(related_symbols)
    route_accuracy = 1.0 if not expected_intent or actual_intent == expected_intent else 0.0
    entity_accuracy = 1.0 if not expected_entity or expected_entity in actual_entities or report.get("symbol") == expected_entity else 0.0
    relation_recall = (hits / len(expected)) if expected else (1.0 if related_symbols or not expected else 0.0)
    evidence_precision = len(available_evidence) / len(all_evidence) if all_evidence else 1.0
    return {
        "symbol": report.get("symbol"),
        "status": report.get("status"),
        "actualIntentType": actual_intent,
        "expectedIntentType": expected_intent,
        "routeAccuracy": route_accuracy,
        "actualEntities": actual_entities,
        "expectedEntitySymbol": expected_entity,
        "entityAccuracy": entity_accuracy,
        "graphExpansionCacheHit": bool(timing.get("graphExpansionCacheHit")),
        "relatedSymbolsReturned": related_symbols,
        "expectedRelatedSymbols": sorted(expected),
        "relationRecall": round(float(relation_recall), 3),
        "evidencePrecision": round(float(evidence_precision), 3),
        "newsItemsFetched": int(timing.get("newsItemsFetched") or 0),
        "crossSignals": int(timing.get("crossSignals") or 0),
        "totalMs": float(timing.get("totalMs") or 0.0),
    }


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(float(item) for item in values) / len(values), 3)


if __name__ == "__main__":
    sys.exit(main())
