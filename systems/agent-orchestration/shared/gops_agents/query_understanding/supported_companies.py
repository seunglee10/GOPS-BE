from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


DEFAULT_COMPANY_CATALOG_PATH = "systems/agent-orchestration/config/supported-companies.json"
DEFAULT_MARKET_SYMBOL_REGISTRY_PATH = "systems/market-data/config/sp500-universe.json"


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
    symbols, source, version = load_market_registry_symbols()
    return SupportedCompanyCatalog(symbols=frozenset(symbols), source=source, version=version, strict=False)


def is_supported_company_symbol(symbol: Any) -> bool:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return False
    catalog = supported_company_catalog()
    if normalized in catalog.symbols:
        return True
    if catalog.strict:
        return False
    if explicit_market_registry_path_configured():
        return False
    return market_symbol_registry_supports(normalized)


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


def load_market_registry_symbols() -> tuple[list[str], str, str]:
    symbols: list[str] = []
    sources: list[str] = []
    versions: list[str] = []
    for path in market_registry_paths():
        loaded = load_symbols_from_json(path) if path.exists() else []
        if not loaded:
            continue
        symbols.extend(loaded)
        sources.append(str(path))
        versions.append(f"{path}:{int(path.stat().st_mtime)}")
    if not explicit_market_registry_path_configured():
        for symbol in configured_market_registry_symbols():
            symbols.append(symbol)
    unique = unique_symbols(symbols)
    return (
        unique,
        "+".join(sources) if sources else "market-data-symbol-registry",
        "|".join(versions) if versions else "market-data-symbol-registry",
    )


def market_registry_paths() -> list[Path]:
    explicit_values = [os.getenv("AGENT_MARKET_SYMBOL_REGISTRY_PATH"), os.getenv("SP500_UNIVERSE_REGISTRY_PATH")]
    values = [value for value in explicit_values if value] or [DEFAULT_MARKET_SYMBOL_REGISTRY_PATH]
    paths = []
    seen = set()
    for value in values:
        if not value:
            continue
        path = resolve_catalog_path(value)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def explicit_market_registry_path_configured() -> bool:
    return bool(os.getenv("AGENT_MARKET_SYMBOL_REGISTRY_PATH") or os.getenv("SP500_UNIVERSE_REGISTRY_PATH"))


def configured_market_registry_symbols() -> list[str]:
    try:
        from alfaka.alpaca.subscription import configured_seed_symbols, configured_universe_symbols, load_universe_registry_symbols
        from alfaka.serving.symbol_registry import SymbolRegistry

        symbols: list[str] = []
        for loader in (configured_universe_symbols, load_universe_registry_symbols, configured_seed_symbols):
            try:
                symbols.extend(loader())
            except Exception:
                continue
        try:
            records = SymbolRegistry().search("", int(os.getenv("AGENT_MARKET_SYMBOL_REGISTRY_LIMIT", "10000")))
            symbols.extend(record.get("symbol") for record in records if isinstance(record, dict))
        except Exception:
            pass
        return unique_symbols(symbols)
    except Exception:
        return []


@lru_cache(maxsize=2048)
def market_symbol_registry_supports(symbol: str) -> bool:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return False
    try:
        from alfaka.serving.symbol_registry import SymbolRegistry

        SymbolRegistry().detail(normalized)
        return True
    except Exception:
        return False


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
