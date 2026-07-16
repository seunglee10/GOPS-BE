from __future__ import annotations


UNSAFE_SIMULATION_PREFIXES = (
    "/api/market/",
    "/api/agent/",
    "/api/agents/",
    "/api/ai-coach/",
    "/api/llm/",
    "/api/charts/analysis-assets",
    "/api/charts/backfill",
    "/api/charts/compare",
    "/api/charts/events",
    "/api/charts/hot-symbols",
    "/api/charts/indicators",
    "/api/charts/order-flow",
    "/api/charts/rankings",
    "/api/charts/volume-profile-bins",
    "/api/charts/watchlist",
    "/api/recommendations/stocks",
)


def requires_point_in_time_data(path: str) -> bool:
    """Return whether a route can expose information after the replay cursor."""

    return any(path.startswith(prefix) for prefix in UNSAFE_SIMULATION_PREFIXES)
