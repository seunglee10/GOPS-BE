from __future__ import annotations

import os
from typing import Any


class EntityCandidateReranker:
    """Optional warm-path hook. Disabled by default for hot-path determinism."""

    def __init__(self, enabled: bool | None = None):
        self.enabled = bool_env("AGENT_ENTITY_LLM_RERANK_ENABLED", False) if enabled is None else enabled

    def choose(self, query: str, candidates: list[dict[str, Any]]) -> str | None:
        if not self.enabled or len(candidates) < 2:
            return None
        return None


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
