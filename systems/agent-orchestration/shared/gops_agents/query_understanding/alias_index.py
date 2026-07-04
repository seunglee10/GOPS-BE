from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable

from .catalog import EntityAliasRecord, EntityCatalog, default_entity_catalog
from .korean_text import choseong_key, compact_text, jamo_key, query_fragments
from .topics import intent_contains_alias


@dataclass(frozen=True)
class EntityAlias:
    alias: str
    symbol: str
    canonical_name: str
    source: str = "catalog"
    priority: float = 1.0
    entity_type: str = "company"
    entity_id: str = ""
    confidence: float = 1.0
    alias_type: str = "name"
    theme_name: str | None = None
    theme_symbols: tuple[str, ...] = ()
    theme_category: str | None = None

    @property
    def compact(self) -> str:
        return compact_text(self.alias)

    @property
    def choseong(self) -> str:
        return choseong_key(self.alias)

    @property
    def jamo(self) -> str:
        return jamo_key(self.alias)


class EntityAliasIndex:
    def __init__(self, aliases: Iterable[EntityAlias], known_symbols: Iterable[str] = ()):
        self.aliases = tuple(aliases)
        self.known_symbols = frozenset(str(symbol).upper() for symbol in known_symbols if str(symbol or "").strip())
        self.by_compact: dict[str, list[EntityAlias]] = defaultdict(list)
        self.by_choseong: dict[str, list[EntityAlias]] = defaultdict(list)
        self.by_length: dict[int, list[EntityAlias]] = defaultdict(list)
        self.ascii_aliases: list[EntityAlias] = []
        for alias in self.aliases:
            if alias.compact:
                self.by_compact[alias.compact].append(alias)
                self.by_length[len(alias.compact)].append(alias)
            if alias.choseong:
                self.by_choseong[alias.choseong].append(alias)
            if re.fullmatch(r"[a-z0-9 .&-]+", str(alias.alias).strip().lower()):
                self.ascii_aliases.append(alias)

    @classmethod
    def from_catalog(cls, catalog: EntityCatalog) -> EntityAliasIndex:
        aliases = [entity_alias_from_record(record) for record in catalog.aliases]
        return cls(aliases, catalog.known_symbols)

    @classmethod
    def from_records(cls, records: Iterable[EntityAliasRecord], known_symbols: Iterable[str] = ()) -> EntityAliasIndex:
        aliases = [entity_alias_from_record(record) for record in records]
        return cls(aliases, known_symbols)

    def exact_matches(self, text: str) -> list[EntityAlias]:
        compacted = compact_text(text)
        fragments = set(query_fragments(text, min_length=2, max_length=32))
        if compacted:
            fragments.add(compacted)
        matches: list[EntityAlias] = []
        seen: set[tuple[str, str, str]] = set()
        for fragment in fragments:
            for alias in self.by_compact.get(fragment, []):
                add_unique_alias(matches, seen, alias)
        normalized = str(text or "").strip().lower()
        for alias in self.ascii_aliases:
            if intent_contains_alias(normalized, compacted, alias.alias):
                add_unique_alias(matches, seen, alias)
        return longest_alias_matches(matches)

    def choseong_matches(self, tokens: Iterable[str]) -> list[tuple[str, EntityAlias]]:
        matches: list[tuple[str, EntityAlias]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for token in tokens:
            if len(token) < 3:
                continue
            for alias in self.by_choseong.get(token, []):
                if len(alias.choseong) < 3:
                    continue
                key = (token, alias.entity_id, alias.symbol, alias.alias)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((token, alias))
        return matches

    def fuzzy_aliases(self, fragment: str) -> Iterable[EntityAlias]:
        compact = compact_text(fragment)
        if not compact:
            return ()
        aliases = []
        for size in range(max(2, len(compact) - 2), len(compact) + 3):
            aliases.extend(self.by_length.get(size, []))
        return aliases

    def theme_matches(self, text: str, *, scorer: Callable[[str, str], float] | None = None) -> list[EntityAlias]:
        exact = [alias for alias in self.exact_matches(text) if alias.entity_type == "theme"]
        if exact or scorer is None:
            return exact
        matches = []
        for fragment in query_fragments(text, min_length=2, max_length=18):
            fragment_compact = compact_text(fragment)
            for alias in self.fuzzy_aliases(fragment):
                if alias.entity_type != "theme" or not alias.compact:
                    continue
                if abs(len(fragment_compact) - len(alias.compact)) > 2:
                    continue
                if scorer(fragment_compact, alias.compact) >= 0.82:
                    matches.append(alias)
        return unique_aliases(matches)


def entity_alias_from_record(record: EntityAliasRecord) -> EntityAlias:
    return EntityAlias(
        alias=record.alias,
        symbol=str(record.symbol or "").upper(),
        canonical_name=record.canonical_name,
        source=record.source,
        priority=record.priority,
        entity_type=record.entity_type,
        entity_id=record.entity_id,
        confidence=record.confidence,
        alias_type=record.alias_type,
        theme_name=record.theme_name,
        theme_symbols=tuple(record.theme_symbols),
        theme_category=record.theme_category,
    )


def add_unique_alias(matches: list[EntityAlias], seen: set[tuple[str, str, str]], alias: EntityAlias) -> None:
    key = (alias.entity_id, alias.symbol, alias.alias)
    if key in seen:
        return
    seen.add(key)
    matches.append(alias)


def unique_aliases(values: Iterable[EntityAlias]) -> list[EntityAlias]:
    matches: list[EntityAlias] = []
    seen: set[tuple[str, str, str]] = set()
    for alias in values:
        add_unique_alias(matches, seen, alias)
    return matches


def longest_alias_matches(values: list[EntityAlias]) -> list[EntityAlias]:
    if not values:
        return []
    max_length = max(len(alias.compact) for alias in values)
    return [alias for alias in values if len(alias.compact) == max_length]


@lru_cache(maxsize=1)
def default_alias_index() -> EntityAliasIndex:
    catalog = default_entity_catalog()
    known_symbols = set(catalog.known_symbols)
    try:
        from .supported_companies import supported_company_catalog

        known_symbols.update(supported_company_catalog().symbols)
    except Exception:
        pass
    aliases = [entity_alias_from_record(record) for record in catalog.aliases]
    return EntityAliasIndex(aliases, known_symbols)
