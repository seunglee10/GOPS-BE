from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

from ..contracts import RoutePlan


@dataclass
class RelatedSymbol:
    symbol: str
    relation_type: str = "unknown"
    score: float = 0.0
    reason: str = ""
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RelatedTheme:
    name: str
    category: str | None = None
    score: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphExpansion:
    source: str = "none"
    cache_hit: bool = False
    generated_at: str | None = None
    relation_version: str | None = None
    related_symbols: list[RelatedSymbol] = field(default_factory=list)
    themes: list[RelatedTheme] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "cache_hit": self.cache_hit,
            "generated_at": self.generated_at,
            "relation_version": self.relation_version,
            "related_symbols": [item.to_dict() for item in self.related_symbols],
            "themes": [item.to_dict() for item in self.themes],
            "keywords": list(self.keywords),
            "warnings": list(self.warnings),
        }


@dataclass
class FanoutPolicy:
    max_related_symbols: int = 3
    max_themes: int = 2
    max_news_items_total: int = 12
    max_market_peers: int = 3
    graph_cache_deadline_ms: int = 80
    expanded_retrieval_deadline_ms: int = 350
    snapshot_total_deadline_ms: int = 900

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetrievalContext:
    run_id: str
    primary_symbol: str
    intent: str
    time_window: str
    route_confidence: float
    graph_expansion: GraphExpansion = field(default_factory=GraphExpansion)
    fanout_policy: FanoutPolicy = field(default_factory=FanoutPolicy)

    def related_symbol_values(self) -> list[str]:
        values = []
        for item in self.graph_expansion.related_symbols[: self.fanout_policy.max_related_symbols]:
            symbol = str(item.symbol or "").strip().upper()
            if symbol and symbol != self.primary_symbol and symbol not in values:
                values.append(symbol)
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "primary_symbol": self.primary_symbol,
            "intent": self.intent,
            "time_window": self.time_window,
            "route_confidence": self.route_confidence,
            "graph_expansion": self.graph_expansion.to_dict(),
            "fanout_policy": self.fanout_policy.to_dict(),
        }


def build_primary_retrieval_context(run_id: str, context: Any, route_plan: RoutePlan) -> RetrievalContext:
    primary_symbol = str(getattr(context, "symbol", "") or "UNKNOWN").upper()
    graph_expansion = graph_expansion_for_symbol(primary_symbol)
    return RetrievalContext(
        run_id=run_id,
        primary_symbol=primary_symbol,
        intent=route_plan.intent,
        time_window=time_window_for_intent(route_plan.intent),
        route_confidence=float(route_plan.route_confidence),
        graph_expansion=graph_expansion,
        fanout_policy=fanout_policy_from_env(),
    )


def graph_expansion_for_symbol(symbol: str) -> GraphExpansion:
    if not bool_env("AGENT_GRAPH_EXPANSION_CACHE_ENABLED", False):
        return GraphExpansion(source="none", cache_hit=False, warnings=["graph_expansion_primary_only"])
    try:
        from .graph_expansion import load_graph_expansion

        return load_graph_expansion(symbol)
    except Exception:
        return GraphExpansion(source="none", cache_hit=False, warnings=["graph_expansion_unavailable"])


def fanout_policy_from_env() -> FanoutPolicy:
    return FanoutPolicy(
        max_related_symbols=env_int("AGENT_MAX_RELATED_SYMBOLS", 3),
        max_themes=env_int("AGENT_MAX_RELATED_THEMES", 2),
        max_news_items_total=env_int("AGENT_MAX_NEWS_ITEMS_TOTAL", 12),
        max_market_peers=env_int("AGENT_MAX_MARKET_PEERS", 3),
        graph_cache_deadline_ms=env_int("AGENT_GRAPH_CACHE_DEADLINE_MS", 80),
        expanded_retrieval_deadline_ms=env_int("AGENT_EXPANDED_RETRIEVAL_DEADLINE_MS", 350),
        snapshot_total_deadline_ms=env_int("AGENT_SNAPSHOT_TOTAL_DEADLINE_MS", 900),
    )


def time_window_for_intent(intent: str) -> str:
    normalized = str(intent or "").strip().lower()
    if "intraday" in normalized or "market" in normalized:
        return "intraday"
    if "news" in normalized:
        return "7d"
    return "1d"


def env_int(name: str, default: int) -> int:
    try:
        parsed = int(os.getenv(name, str(default)))
    except Exception:
        return default
    return parsed if parsed > 0 else default


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
