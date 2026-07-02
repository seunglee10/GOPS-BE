from __future__ import annotations

import re
from typing import Any

from .korean_text import compact_text, normalize_query_text
from .seeds import NEWS_TOPIC_BASKETS


def extract_news_topic_from_intent(intent: str) -> dict[str, Any] | None:
    normalized = normalize_query_text(intent)
    compacted = compact_text(normalized)
    if not is_topic_news_request(normalized, compacted):
        return None
    for alias in dynamic_theme_matches(intent):
        symbols = tuple(symbol for symbol in alias.theme_symbols if symbol)
        if symbols:
            return {
                "label": alias.theme_name or alias.canonical_name,
                "symbols": symbols,
                "source": alias.source,
                "entityId": alias.entity_id,
            }
    return None


def extract_theme_names_from_intent(intent: str) -> list[str]:
    names = []
    for alias in dynamic_theme_matches(intent):
        name = alias.theme_name or alias.canonical_name
        if name and name not in names:
            names.append(name)
    return names


def is_topic_news_request(normalized: str, compacted: str) -> bool:
    news_terms = ("뉴스", "기사", "헤드라인", "news", "article", "headline")
    topic_terms = ("관련", "섹터", "테마", "업종", "industry", "sector", "theme")
    has_news_term = any(term in compacted for term in news_terms)
    if not has_news_term:
        return False
    if any(term in compacted for term in topic_terms):
        return True
    return bool(dynamic_theme_matches(normalized))


def dynamic_theme_matches(intent: str):
    try:
        from .alias_index import default_alias_index
        from .entity_resolver import fuzzy_ratio

        return default_alias_index().theme_matches(intent, scorer=fuzzy_ratio)
    except Exception:
        return []


def intent_contains_alias(normalized: str, compacted: str, alias: str) -> bool:
    normalized_alias = normalize_query_text(alias)
    if re.fullmatch(r"[a-z0-9 .&-]+", normalized_alias):
        pattern = re.escape(normalized_alias).replace(r"\ ", r"\s+")
        return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", normalized))
    return compact_text(normalized_alias) in compacted
