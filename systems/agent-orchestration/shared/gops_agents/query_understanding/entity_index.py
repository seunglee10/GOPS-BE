from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from .alias_index import EntityAlias, default_alias_index
from .seeds import COMPANY_SYMBOL_ALIASES


@lru_cache(maxsize=1)
def default_entity_index() -> tuple[EntityAlias, ...]:
    return default_alias_index().aliases


@lru_cache(maxsize=1)
def known_agent_symbols() -> frozenset[str]:
    symbols = set(default_alias_index().known_symbols)
    symbols.update(alias.symbol for alias in default_entity_index() if alias.symbol)
    return frozenset(symbols)


def aliases_for_symbols(symbols: Iterable[str]) -> tuple[EntityAlias, ...]:
    wanted = {str(symbol or "").upper() for symbol in symbols}
    return tuple(alias for alias in default_entity_index() if alias.symbol in wanted)
