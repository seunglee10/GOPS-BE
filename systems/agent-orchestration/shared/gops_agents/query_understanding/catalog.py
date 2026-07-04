from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .korean_text import compact_text
from .seeds import COMPANY_SYMBOL_ALIASES, EXTRA_KNOWN_SYMBOLS, NEWS_TOPIC_BASKETS


DEFAULT_ALIAS_CATALOG_PATH = "systems/agent-orchestration/config/entity-aliases.json"
DEFAULT_ALIAS_SEED_PATH = "systems/agent-orchestration/config/entity-aliases.seed.json"


@dataclass(frozen=True)
class CatalogEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    symbol: str | None = None
    market: str | None = "US"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    source: str = "catalog"
    priority: float = 1.0
    updated_at: str | None = None
    theme_symbols: tuple[str, ...] = field(default_factory=tuple)
    theme_category: str | None = None


@dataclass(frozen=True)
class EntityAliasRecord:
    alias: str
    entity_id: str
    entity_type: str
    canonical_name: str
    symbol: str | None = None
    language: str | None = None
    alias_type: str = "name"
    source: str = "catalog"
    confidence: float = 1.0
    priority: float = 1.0
    theme_name: str | None = None
    theme_symbols: tuple[str, ...] = field(default_factory=tuple)
    theme_category: str | None = None


@dataclass(frozen=True)
class EntityCatalog:
    entities: tuple[CatalogEntity, ...]
    aliases: tuple[EntityAliasRecord, ...]
    known_symbols: frozenset[str]
    source_counts: dict[str, int] = field(default_factory=dict)


class EntityCatalogProvider:
    def __init__(
        self,
        *,
        clickhouse_provider: Any = None,
        sparql_client: Any = None,
        alias_catalog_path: str | os.PathLike[str] | None = None,
        alias_seed_path: str | os.PathLike[str] | None = None,
        include_fallback_seed: bool = True,
        include_dynamic_company_sources: bool | None = None,
        strict: bool | None = None,
        symbol_limit: int | None = None,
        theme_limit: int | None = None,
    ):
        self.clickhouse_provider = clickhouse_provider
        self.sparql_client = sparql_client
        self.alias_catalog_path = resolve_catalog_path(alias_catalog_path or os.getenv("AGENT_ENTITY_ALIAS_CATALOG_PATH", DEFAULT_ALIAS_CATALOG_PATH))
        self.alias_seed_path = resolve_catalog_path(alias_seed_path or os.getenv("AGENT_ENTITY_ALIAS_SEED_PATH", DEFAULT_ALIAS_SEED_PATH))
        self.include_fallback_seed = include_fallback_seed
        self.include_dynamic_company_sources = (
            bool_config("AGENT_ENTITY_CATALOG_DYNAMIC_COMPANIES_ENABLED", clickhouse_provider is not None)
            if include_dynamic_company_sources is None
            else bool(include_dynamic_company_sources)
        )
        self.strict = bool_config("AGENT_ENTITY_CATALOG_STRICT", False) if strict is None else bool(strict)
        self.symbol_limit = int(symbol_limit or os.getenv("AGENT_ENTITY_CATALOG_SYMBOL_LIMIT", "10000"))
        self.theme_limit = int(theme_limit or os.getenv("AGENT_ENTITY_CATALOG_THEME_LIMIT", "10000"))

    def load(self) -> EntityCatalog:
        builder = EntityCatalogBuilder()
        catalog_entities = self._entities_from_alias_artifact(self.alias_catalog_path, required=self.strict)
        for entity in catalog_entities:
            builder.add_entity(entity)
        for entity in self._theme_entities_from_graphdb():
            builder.add_entity(entity)
        if self.include_dynamic_company_sources:
            for entity in self._symbol_entities_from_clickhouse():
                builder.add_entity(entity)
            for entity in self._symbol_entities_from_market_config():
                builder.add_entity(entity)
        if not catalog_entities and self.include_fallback_seed:
            seed_entities = self._entities_from_alias_artifact(self.alias_seed_path, required=False)
            for entity in seed_entities or fallback_seed_entities():
                builder.add_entity(entity)
        return builder.build()

    def _symbol_entities_from_clickhouse(self) -> list[CatalogEntity]:
        provider = self.clickhouse_provider or default_clickhouse_provider()
        if provider is None:
            return []
        try:
            if hasattr(provider, "symbols"):
                rows = provider.symbols(limit=self.symbol_limit)
            elif hasattr(provider, "query_json_each_row"):
                table = provider.table("symbols") if hasattr(provider, "table") else "market_data.symbols"
                rows = provider.query_json_each_row(
                    f"""
                    SELECT
                      symbol,
                      name,
                      exchange,
                      market,
                      asset_class AS assetClass,
                      source,
                      formatDateTime(updated_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS updatedAt
                    FROM {table}
                    ORDER BY symbol ASC
                    LIMIT {{limit:UInt32}}
                    FORMAT JSONEachRow
                    """,
                    {"limit": self.symbol_limit},
                )
            elif hasattr(provider, "search_symbols"):
                rows = provider.search_symbols("", self.symbol_limit)
            else:
                return []
        except Exception:
            return []
        return [entity_from_symbol_row(row, source="clickhouse-symbols") for row in rows if isinstance(row, dict)]

    def _symbol_entities_from_market_config(self) -> list[CatalogEntity]:
        try:
            from alfaka.alpaca.subscription import configured_universe_symbols, load_request_config

            config = load_request_config()
            symbols = configured_universe_symbols(config)
        except Exception:
            return []
        metadata = config.get("symbolMetadata") or {}
        company_to_symbol = config.get("companyToSymbol") or {}
        aliases_by_symbol: dict[str, list[str]] = defaultdict(list)
        for alias, symbol in company_to_symbol.items():
            normalized_symbol = normalize_symbol(symbol)
            if normalized_symbol:
                aliases_by_symbol[normalized_symbol].append(str(alias))
        entities = []
        for symbol in symbols[: self.symbol_limit]:
            normalized_symbol = normalize_symbol(symbol)
            if not normalized_symbol:
                continue
            raw = metadata.get(normalized_symbol) or {}
            name = str(raw.get("name") or normalized_symbol)
            aliases = unique_strings([normalized_symbol, name, *aliases_from_company_name(name), *aliases_by_symbol.get(normalized_symbol, [])])
            entities.append(
                CatalogEntity(
                    entity_id=f"company:{normalized_symbol}",
                    entity_type="company",
                    symbol=normalized_symbol,
                    canonical_name=name,
                    market=str(raw.get("market") or raw.get("exchange") or "US"),
                    aliases=tuple(aliases),
                    source="market-config",
                    priority=0.8,
                )
            )
        return entities

    def _theme_entities_from_graphdb(self) -> list[CatalogEntity]:
        client = self.sparql_client or default_sparql_client()
        if client is None:
            return []
        try:
            rows = sparql_json_to_rows(client.query(theme_catalog_query(self.theme_limit)))
        except Exception:
            return []
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("themeName") or "").strip()
            if not name:
                continue
            item = grouped.setdefault(name, {"symbols": [], "category": row.get("themeCategory")})
            symbol = normalize_symbol(row.get("ticker"))
            if symbol and symbol not in item["symbols"]:
                item["symbols"].append(symbol)
            if row.get("themeCategory"):
                item["category"] = row.get("themeCategory")
        entities = []
        for theme_name, value in grouped.items():
            aliases = unique_strings([theme_name, *split_theme_aliases(theme_name), str(value.get("category") or "")])
            entities.append(
                CatalogEntity(
                    entity_id=f"theme:{compact_text(theme_name)}",
                    entity_type="theme",
                    canonical_name=theme_name,
                    aliases=tuple(aliases),
                    source="graphdb-themes",
                    priority=0.95,
                    theme_symbols=tuple(value["symbols"]),
                    theme_category=string_or_none(value.get("category")),
                )
            )
        return entities

    def _entities_from_alias_artifact(self, path: Path, *, required: bool) -> list[CatalogEntity]:
        if not path.exists():
            if required:
                raise FileNotFoundError(f"Entity alias catalog does not exist: {path}")
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            if required:
                raise ValueError(f"Entity alias catalog is not valid JSON: {path}") from exc
            return []
        if not isinstance(payload, dict):
            if required:
                raise ValueError(f"Entity alias catalog must be a JSON object: {path}")
            return []
        entities = []
        for item in payload.get("companies") or []:
            if not isinstance(item, dict):
                continue
            symbol = normalize_symbol(item.get("symbol"))
            if not symbol:
                continue
            canonical_name = str(item.get("canonicalName") or item.get("canonical_name") or item.get("name") or symbol)
            aliases = unique_strings([symbol, canonical_name, *aliases_from_company_name(canonical_name), *(item.get("aliases") or [])])
            entities.append(
                CatalogEntity(
                    entity_id=str(item.get("entityId") or item.get("entity_id") or f"company:{symbol}"),
                    entity_type="company",
                    symbol=symbol,
                    canonical_name=canonical_name,
                    market=string_or_none(item.get("market")) or "US",
                    aliases=tuple(aliases),
                    source=str(item.get("source") or "alias-artifact"),
                    priority=float_or_default(item.get("priority"), 1.0),
                    updated_at=string_or_none(item.get("updatedAt") or item.get("updated_at")),
                )
            )
        for item in payload.get("themes") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("themeName") or item.get("theme_name") or "").strip()
            if not name:
                continue
            symbols = tuple(unique_symbols(item.get("symbols") or item.get("themeSymbols") or item.get("theme_symbols") or []))
            aliases = unique_strings([name, *split_theme_aliases(name), *(item.get("aliases") or [])])
            entities.append(
                CatalogEntity(
                    entity_id=str(item.get("entityId") or item.get("entity_id") or f"theme:{compact_text(name)}"),
                    entity_type="theme",
                    canonical_name=name,
                    aliases=tuple(aliases),
                    source=str(item.get("source") or "alias-artifact"),
                    priority=float_or_default(item.get("priority"), 1.0),
                    updated_at=string_or_none(item.get("updatedAt") or item.get("updated_at")),
                    theme_symbols=symbols,
                    theme_category=string_or_none(item.get("category") or item.get("themeCategory") or item.get("theme_category")),
                )
            )
        return entities


class EntityCatalogBuilder:
    def __init__(self):
        self.entities: dict[str, CatalogEntity] = {}
        self.aliases: dict[tuple[str, str, str], EntityAliasRecord] = {}
        self.source_counts: dict[str, int] = defaultdict(int)

    def add_entity(self, entity: CatalogEntity) -> None:
        if not entity.entity_id or not entity.entity_type:
            return
        current = self.entities.get(entity.entity_id)
        if current is None or entity.priority >= current.priority:
            self.entities[entity.entity_id] = entity
        self.source_counts[entity.source] += 1
        for alias in entity.aliases:
            self.add_alias(entity, alias)

    def add_alias(self, entity: CatalogEntity, alias: str) -> None:
        normalized_alias = str(alias or "").strip()
        if not normalized_alias:
            return
        key = (compact_text(normalized_alias), entity.entity_id, normalize_symbol(entity.symbol))
        if key in self.aliases:
            return
        self.aliases[key] = EntityAliasRecord(
            alias=normalized_alias,
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            symbol=normalize_symbol(entity.symbol) or None,
            canonical_name=entity.canonical_name,
            language=guess_language(normalized_alias),
            alias_type="ticker" if normalize_symbol(entity.symbol) == normalized_alias.upper() else "name",
            source=entity.source,
            confidence=min(1.0, max(0.0, entity.priority)),
            priority=entity.priority,
            theme_name=entity.canonical_name if entity.entity_type == "theme" else None,
            theme_symbols=entity.theme_symbols,
            theme_category=entity.theme_category,
        )

    def build(self) -> EntityCatalog:
        known_symbols = frozenset(
            symbol
            for symbol in (normalize_symbol(entity.symbol) for entity in self.entities.values())
            if symbol
        )
        return EntityCatalog(
            entities=tuple(self.entities.values()),
            aliases=tuple(self.aliases.values()),
            known_symbols=known_symbols,
            source_counts=dict(self.source_counts),
        )


def fallback_seed_entities() -> list[CatalogEntity]:
    by_symbol: dict[str, list[str]] = defaultdict(list)
    for alias, symbol in COMPANY_SYMBOL_ALIASES:
        by_symbol[normalize_symbol(symbol)].append(alias)
    entities = []
    for symbol, aliases in by_symbol.items():
        if not symbol:
            continue
        entities.append(
            CatalogEntity(
                entity_id=f"company:{symbol}",
                entity_type="company",
                symbol=symbol,
                canonical_name=symbol,
                aliases=tuple(unique_strings([symbol, *aliases])),
                source="fallback-seed",
                priority=0.9,
            )
        )
    for topic in NEWS_TOPIC_BASKETS:
        label = str(topic.get("label") or "").strip()
        if not label:
            continue
        entities.append(
            CatalogEntity(
                entity_id=f"theme:{compact_text(label)}",
                entity_type="theme",
                canonical_name=label,
                aliases=tuple(unique_strings([label, *(topic.get("aliases") or [])])),
                source="fallback-seed",
                priority=0.85,
                theme_symbols=tuple(unique_symbols(topic.get("symbols") or [])),
            )
        )
    for symbol in EXTRA_KNOWN_SYMBOLS:
        normalized_symbol = normalize_symbol(symbol)
        if not normalized_symbol:
            continue
        entities.append(
            CatalogEntity(
                entity_id=f"company:{normalized_symbol}",
                entity_type="company",
                symbol=normalized_symbol,
                canonical_name=normalized_symbol,
                aliases=(normalized_symbol,),
                source="fallback-known-symbol",
                priority=0.9,
            )
        )
    return entities


def entity_from_symbol_row(row: dict[str, Any], *, source: str) -> CatalogEntity:
    symbol = normalize_symbol(row.get("symbol"))
    name = str(row.get("name") or symbol)
    aliases = unique_strings([symbol, name, *aliases_from_company_name(name)])
    return CatalogEntity(
        entity_id=f"company:{symbol}",
        entity_type="company",
        symbol=symbol,
        canonical_name=name,
        market=string_or_none(row.get("market") or row.get("exchange")) or "US",
        aliases=tuple(aliases),
        source=source,
        priority=0.9,
        updated_at=string_or_none(row.get("updatedAt") or row.get("updated_at")),
    )


def aliases_from_company_name(name: str) -> list[str]:
    text = str(name or "").strip()
    if not text:
        return []
    cleaned = re.sub(r"\b(incorporated|corporation|corp|inc|company|co|ltd|limited|class [a-z])\.?\b", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[, ]+", " ", cleaned).strip(" .,-")
    values = [cleaned]
    if "," in text:
        values.append(text.split(",", 1)[0].strip())
    return unique_strings(values)


def split_theme_aliases(theme_name: str) -> list[str]:
    parts = re.split(r"[/,|]", str(theme_name or ""))
    return [part.strip() for part in parts if part.strip()]


def theme_catalog_query(limit: int) -> str:
    return f"""
PREFIX gops: <urn:gops:ontology:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?ticker ?companyName ?sector ?themeName ?themeCategory
WHERE {{
  GRAPH <urn:gops:graph:themes:current> {{
    ?company gops:hasTheme ?theme .
    ?theme gops:themeNameKo ?themeName .
    OPTIONAL {{ ?theme gops:themeCategory ?themeCategory . }}
  }}
  ?company gops:ticker ?ticker .
  OPTIONAL {{ ?company rdfs:label ?companyName . }}
  OPTIONAL {{ ?company gops:sector ?sector . }}
}}
ORDER BY ?themeName ?ticker
LIMIT {max(1, min(int(limit), 50000))}
""".strip()


def sparql_json_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = payload.get("results", {}).get("bindings", [])
    rows = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        rows.append({
            key: value.get("value")
            for key, value in binding.items()
            if isinstance(value, dict) and value.get("value") is not None
        })
    return rows


class GraphCatalogSparqlClient:
    def __init__(self, sparql_url: str, timeout_seconds: float):
        self.sparql_url = sparql_url
        self.timeout_seconds = timeout_seconds

    def query(self, sparql: str) -> dict[str, Any]:
        import requests

        response = requests.get(
            self.sparql_url,
            params={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()


def default_clickhouse_provider() -> Any:
    if not os.getenv("CLICKHOUSE_HTTP_URL"):
        return None
    try:
        from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

        return ClickHouseMarketDataProvider()
    except Exception:
        return None


def default_sparql_client() -> Any:
    sparql_url = os.getenv("GRAPHDB_SPARQL_URL")
    if not sparql_url:
        return None
    timeout_seconds = float(os.getenv("AGENT_GRAPHDB_TIMEOUT_MS", "500")) / 1000
    return GraphCatalogSparqlClient(sparql_url, timeout_seconds)


@lru_cache(maxsize=1)
def default_entity_catalog() -> EntityCatalog:
    return EntityCatalogProvider().load()


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def unique_symbols(values: Iterable[Any]) -> list[str]:
    result = []
    for value in values:
        symbol = normalize_symbol(value)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def unique_strings(values: Iterable[Any]) -> list[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def bool_config(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def guess_language(value: str) -> str:
    text = str(value or "")
    if re.search(r"[가-힣ㄱ-ㅎㅏ-ㅣ]", text):
        return "ko"
    if re.search(r"[a-zA-Z]", text):
        return "en"
    return "und"


def resolve_catalog_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    repo_root = Path(__file__).resolve().parents[5]
    rooted = repo_root / path
    return rooted
