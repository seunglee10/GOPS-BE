from __future__ import annotations

import json
import os
import statistics
import sys
from typing import Any

from gops_agents.orchestrator import AgentOrchestrator


DEFAULT_QUERIES = [
    {"symbol": "NVDA", "intent": "NVDA 왜 올랐어?", "agentIds": ["agent-02"]},
    {"symbol": "AAPL", "intent": "AAPL 뉴스 보여줘", "agentIds": ["agent-02"]},
    {"symbol": "MSFT", "intent": "MSFT 시장 요약", "agentIds": ["agent-01", "agent-02"]},
]


def main() -> int:
    queries = query_set_from_env()
    orchestrator = AgentOrchestrator()
    rows = []
    for query in queries:
        report = orchestrator.analyze(query)
        timing = dict(report.timing or {})
        rows.append({
            "symbol": report.symbol,
            "intent": report.intent,
            "status": report.status,
            "totalMs": float(timing.get("totalMs") or 0.0),
            "snapshotFetchMs": float(timing.get("snapshotFetchMs") or 0.0),
            "retrievalContextMs": float(timing.get("retrievalContextMs") or 0.0),
            "crossSignalJoinMs": float(timing.get("crossSignalJoinMs") or 0.0),
            "finalAnswerMs": float(timing.get("finalAnswerMs") or 0.0),
            "graphExpansionCacheHit": bool(timing.get("graphExpansionCacheHit")),
            "relatedSymbolsUsed": int(timing.get("relatedSymbolsUsed") or 0),
        })
    totals = [row["totalMs"] for row in rows]
    burst = run_hot_symbol_burst(orchestrator)
    result = {
        "status": "ok",
        "count": len(rows),
        "p50TotalMs": percentile(totals, 50),
        "p95TotalMs": percentile(totals, 95),
        "rows": rows,
        "hotSymbolBurst": burst,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def query_set_from_env() -> list[dict[str, Any]]:
    raw = os.getenv("AGENT_BENCHMARK_QUERIES_JSON")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except Exception:
            pass
    return list(DEFAULT_QUERIES)


def run_hot_symbol_burst(orchestrator: AgentOrchestrator) -> dict[str, Any]:
    burst_requests = env_int("AGENT_BENCHMARK_BURST_REQUESTS", 0)
    if burst_requests <= 0:
        return {"enabled": False}
    symbol = os.getenv("AGENT_BENCHMARK_BURST_SYMBOL", "NVDA")
    intent = os.getenv("AGENT_BENCHMARK_BURST_INTENT", f"{symbol} 뉴스 영향 분석")
    rows = []
    for index in range(burst_requests):
        report = orchestrator.analyze({"symbol": symbol, "intent": intent, "agentIds": ["agent-02"], "burstIndex": index})
        timing = dict(report.timing or {})
        rows.append({
            "totalMs": float(timing.get("totalMs") or 0.0),
            "cacheHit": bool(timing.get("cacheHit")),
            "cacheLayer": str(timing.get("cacheLayer") or "none"),
            "newsItemsFetched": int(timing.get("newsItemsFetched") or 0),
        })
    totals = [row["totalMs"] for row in rows]
    cache_hits = sum(1 for row in rows if row["cacheHit"])
    return {
        "enabled": True,
        "symbol": symbol,
        "requests": burst_requests,
        "cacheHitRate": round(cache_hits / len(rows), 3) if rows else 0.0,
        "p50TotalMs": percentile(totals, 50),
        "p95TotalMs": percentile(totals, 95),
        "rows": rows,
    }


def env_int(name: str, default: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except Exception:
        return default
    return parsed if parsed >= 0 else default


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 3)
    values = sorted(values)
    if pct == 50:
        return round(float(statistics.median(values)), 3)
    index = min(len(values) - 1, max(0, round((pct / 100) * (len(values) - 1))))
    return round(float(values[index]), 3)


if __name__ == "__main__":
    sys.exit(main())
