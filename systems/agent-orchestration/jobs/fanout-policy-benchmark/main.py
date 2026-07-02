from __future__ import annotations

import json
import os
import sys

from gops_agents.orchestrator import AgentOrchestrator


def main() -> int:
    values = [int(item) for item in os.getenv("AGENT_FANOUT_BENCHMARK_VALUES", "0,1,3").split(",") if item.strip().isdigit()]
    query = {
        "symbol": os.getenv("AGENT_FANOUT_BENCHMARK_SYMBOL", "NVDA"),
        "intent": os.getenv("AGENT_FANOUT_BENCHMARK_INTENT", "NVDA 관련 뉴스 영향 분석"),
        "agentIds": ["agent-02"],
    }
    rows = []
    original = os.environ.get("AGENT_MAX_RELATED_SYMBOLS")
    try:
        for value in values:
            os.environ["AGENT_MAX_RELATED_SYMBOLS"] = str(value)
            report = AgentOrchestrator().analyze(query)
            rows.append({
                "maxRelatedSymbols": value,
                "totalMs": float(report.timing.get("totalMs") or 0.0),
                "relatedSymbolsRequested": int(report.timing.get("relatedSymbolsRequested") or 0),
                "relatedSymbolsUsed": int(report.timing.get("relatedSymbolsUsed") or 0),
                "fanoutTruncated": bool(report.timing.get("fanoutTruncated")),
            })
    finally:
        if original is None:
            os.environ.pop("AGENT_MAX_RELATED_SYMBOLS", None)
        else:
            os.environ["AGENT_MAX_RELATED_SYMBOLS"] = original
    print(json.dumps({"status": "ok", "rows": rows}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
