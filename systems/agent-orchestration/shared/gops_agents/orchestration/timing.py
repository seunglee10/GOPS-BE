from __future__ import annotations

import time
from typing import Any


def empty_timing() -> dict[str, Any]:
    return {
        "totalMs": 0.0,
        "queueWaitMs": 0.0,
        "cacheHit": False,
        "cacheLayer": "none",
        "newsFetchMs": 0.0,
        "routeAndPlanMs": 0.0,
        "entityResolveMs": 0.0,
        "retrievalContextMs": 0.0,
        "snapshotFetchMs": 0.0,
        "crossSignalJoinMs": 0.0,
        "roleAnalysisMs": 0.0,
        "finalAnswerMs": 0.0,
        "guardrailMs": 0.0,
        "llmCalls": 0,
        "llmBudgetBlocked": 0,
        "directNewsCount": 0,
        "mentionNewsCount": 0,
        "newsItemsFetched": 0,
        "crossSignals": 0,
        "graphExpansionCacheHit": False,
        "relatedSymbolsRequested": 0,
        "relatedSymbolsUsed": 0,
        "themesUsed": 0,
        "marketPeersRequested": 0,
        "marketPeersFetched": 0,
        "fanoutTruncated": False,
        "hotWorkerSaturation": False,
        "deepWorkerSaturation": False,
        "providerBulkheadRejected": 0,
    }


def add_timing_ms(state: dict[str, Any], key: str, elapsed_ms: float) -> None:
    timing = state.get("timing")
    if not isinstance(timing, dict):
        return
    current = timing.get(key)
    timing[key] = (float(current) if isinstance(current, (int, float)) else 0.0) + elapsed_ms


def finalize_timing(state: dict[str, Any]) -> dict[str, Any]:
    timing = dict(state.get("timing") if isinstance(state.get("timing"), dict) else empty_timing())
    started_at = state.get("timing_started_at")
    if isinstance(started_at, (int, float)):
        timing["totalMs"] = (time.perf_counter() - started_at) * 1000
    for key in [
        "totalMs",
        "queueWaitMs",
        "newsFetchMs",
        "roleAnalysisMs",
        "finalAnswerMs",
        "routeAndPlanMs",
        "entityResolveMs",
        "retrievalContextMs",
        "snapshotFetchMs",
        "crossSignalJoinMs",
        "guardrailMs",
    ]:
        value = timing.get(key)
        timing[key] = round(float(value) if isinstance(value, (int, float)) else 0.0, 3)
    for key in [
        "llmCalls",
        "llmBudgetBlocked",
        "directNewsCount",
        "mentionNewsCount",
        "newsItemsFetched",
        "relatedSymbolsRequested",
        "relatedSymbolsUsed",
        "themesUsed",
        "marketPeersRequested",
        "marketPeersFetched",
        "crossSignals",
        "providerBulkheadRejected",
    ]:
        value = timing.get(key)
        timing[key] = int(value) if isinstance(value, (int, float)) else 0
    labels = timing.get("llmCallLabels")
    timing["llmCallLabels"] = [str(item) for item in labels] if isinstance(labels, list) else []
    timing["cacheHit"] = bool(timing.get("cacheHit"))
    timing["cacheLayer"] = str(timing.get("cacheLayer") or "none")
    timing["graphExpansionCacheHit"] = bool(timing.get("graphExpansionCacheHit"))
    timing["fanoutTruncated"] = bool(timing.get("fanoutTruncated"))
    timing["hotWorkerSaturation"] = bool(timing.get("hotWorkerSaturation"))
    timing["deepWorkerSaturation"] = bool(timing.get("deepWorkerSaturation"))
    return timing
