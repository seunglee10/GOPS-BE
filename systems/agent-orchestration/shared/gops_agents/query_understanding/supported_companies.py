from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .entity_index import known_agent_symbols


DEFAULT_COMPANY_CATALOG_PATH = "systems/agent-orchestration/config/supported-companies.json"


@dataclass(frozen=True)
class SupportedCompanyCatalog:
    symbols: frozenset[str]
    source: str
    version: str
    strict: bool = False


@lru_cache(maxsize=1)
def supported_company_catalog() -> SupportedCompanyCatalog:
    configured_path = os.getenv("AGENT_COMPANY_CATALOG_PATH") or os.getenv("AGENT_SUPPORTED_COMPANY_CATALOG_PATH")
    path = resolve_catalog_path(configured_path or DEFAULT_COMPANY_CATALOG_PATH)
    if path.exists():
        symbols = load_symbols_from_json(path)
        version = f"{path}:{int(path.stat().st_mtime)}"
        return SupportedCompanyCatalog(symbols=frozenset(symbols), source=str(path), version=version, strict=True)
    if configured_path:
        return SupportedCompanyCatalog(symbols=frozenset(), source=str(path), version="missing", strict=True)
    symbols = frozenset(symbol for symbol in known_agent_symbols() if symbol)
    return SupportedCompanyCatalog(symbols=symbols, source="entity-catalog", version="entity-catalog", strict=False)


def is_supported_company_symbol(symbol: Any) -> bool:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return False
    return normalized in supported_company_catalog().symbols


def supported_company_catalog_payload() -> dict[str, Any]:
    catalog = supported_company_catalog()
    return {
        "source": catalog.source,
        "version": catalog.version,
        "strict": catalog.strict,
        "symbolCount": len(catalog.symbols),
    }


def load_symbols_from_json(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    symbols = []
    if isinstance(payload, list):
        symbols.extend(payload)
    elif isinstance(payload, dict):
        symbols.extend(payload.get("symbols") or [])
        for item in payload.get("companies") or []:
            if isinstance(item, dict):
                symbols.append(item.get("symbol"))
            else:
                symbols.append(item)
    return unique_symbols(symbols)


def unique_symbols(values: Iterable[Any]) -> list[str]:
    result = []
    for value in values:
        symbol = normalize_symbol(value)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def resolve_catalog_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / path
