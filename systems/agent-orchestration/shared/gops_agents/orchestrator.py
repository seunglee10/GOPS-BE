from __future__ import annotations

from .orchestration.cache import canonical_analysis_intent, normalize_cache_intent
from .orchestration.request import (
    latest_message,
    normalize_symbol,
    read_symbol_from_chart_context,
    sanitize_chart_context_for_symbol,
)
from .orchestration.workflow import AgentOrchestrator
from .query_understanding import (
    extract_news_topic_from_intent,
    extract_relationship_symbols_from_intent,
    extract_symbol_from_intent,
    relationship_symbols_for_context,
)
from .query_understanding.entity_index import COMPANY_SYMBOL_ALIASES, known_agent_symbols
from .query_understanding.topics import NEWS_TOPIC_BASKETS, intent_contains_alias, is_topic_news_request


def extract_symbol_alias_from_intent(intent: str) -> str | None:
    return extract_symbol_from_intent(intent)


__all__ = [
    "AgentOrchestrator",
    "COMPANY_SYMBOL_ALIASES",
    "NEWS_TOPIC_BASKETS",
    "canonical_analysis_intent",
    "extract_news_topic_from_intent",
    "extract_relationship_symbols_from_intent",
    "extract_symbol_alias_from_intent",
    "extract_symbol_from_intent",
    "intent_contains_alias",
    "is_topic_news_request",
    "known_agent_symbols",
    "latest_message",
    "normalize_cache_intent",
    "normalize_symbol",
    "read_symbol_from_chart_context",
    "relationship_symbols_for_context",
    "sanitize_chart_context_for_symbol",
]
