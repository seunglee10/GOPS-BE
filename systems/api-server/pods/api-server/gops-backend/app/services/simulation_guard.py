from __future__ import annotations

import re


UNSAFE_SIMULATION_PREFIXES = (
    "/api/market/",
    "/api/agent/",
    "/api/agents/",
    "/api/ai-coach/",
    "/api/company-journal/",
    "/api/llm/",
    "/api/charts/analysis-assets",
    "/api/charts/backfill",
    "/api/charts/compare",
    "/api/charts/hot-symbols",
    "/api/charts/indicators",
    "/api/charts/order-flow",
    "/api/charts/rankings",
    "/api/charts/volume-profile-bins",
    "/api/charts/watchlist",
    "/api/recommendations/stocks",
)

SAFE_SIMULATION_READ_PATHS = frozenset({
    "/api/ai-coach/reports/latest",
    "/api/market/news/latest",
    "/api/market/news/daily",
    "/api/market/indices",
    "/api/market/indices/related",
    "/api/charts/analysis-assets",
    "/api/charts/analysis-assets/commentary",
    "/api/charts/analysis-assets/coverage",
    "/api/charts/indicators",
    "/api/charts/order-flow/symbols",
    "/api/charts/order-flow/intraday",
    "/api/charts/volume-profile-bins",
})

SAFE_SIMULATION_READ_PREFIXES = (
    "/api/company-journal/",
    "/api/charts/analysis-assets/build/",
)


_CUTOFF_SAFE_COMPANY_JOURNAL_EVIDENCE_PATH = re.compile(
    r"^/api/company-journal/[A-Za-z0-9.-]{1,15}/evidence/?$"
)


def supports_cutoff_safe_simulation_read(path: str, method: str = "GET") -> bool:
    """Return whether middleware may attach replay virtualTime and continue safely."""

    if method.upper() == "POST" and (
        path == "/api/charts/analysis-assets/build"
        or (path.startswith("/api/charts/analysis-assets/build/") and path.endswith("/cancel"))
    ):
        # This authenticated developer operation freezes its own dataset/startTime
        # context in the route and only queues a precomputed simulation snapshot.
        return True
    return method.upper() == "GET" and bool(_CUTOFF_SAFE_COMPANY_JOURNAL_EVIDENCE_PATH.fullmatch(path))


def requires_point_in_time_data(path: str, method: str = "GET") -> bool:
    """Return whether a route can expose information after the replay cursor."""

    if method.upper() == "GET":
        if path in SAFE_SIMULATION_READ_PATHS:
            return False
        if any(path.startswith(prefix) for prefix in SAFE_SIMULATION_READ_PREFIXES):
            return False
    return any(path.startswith(prefix) for prefix in UNSAFE_SIMULATION_PREFIXES)
