from __future__ import annotations

import json
import os
from typing import Any

from .contracts import EvidenceItem, utc_now_iso
from .providers import GraphDBOntologyProvider, ProviderRequest
from .retrieval_context import GraphExpansion, RelatedSymbol, RelatedTheme


DEFAULT_GRAPH_EXPANSION_PREFIX = "gops:agent:graph-expansion:v1"
DEFAULT_GRAPH_EXPANSION_TABLE = "agent_graph_expansions"
GraphExpansionHint = GraphExpansion


class GraphExpansionCache:
    def __init__(self, redis_client=None, clickhouse_provider=None):
        self.redis = redis_client
        self.clickhouse_provider = clickhouse_provider

    def load(self, symbol: str) -> GraphExpansion:
        normalized_symbol = normalize_symbol(symbol)
        redis_expansion = self._load_redis(normalized_symbol)
        if redis_expansion is not None:
            redis_expansion.source = "redis"
            redis_expansion.cache_hit = True
            return redis_expansion
        clickhouse_expansion = self._load_clickhouse(normalized_symbol)
        if clickhouse_expansion is not None:
            clickhouse_expansion.source = "clickhouse"
            clickhouse_expansion.cache_hit = False
            return clickhouse_expansion
        return GraphExpansion(source="none", cache_hit=False, warnings=["graph_expansion_unavailable"])

    def _load_redis(self, symbol: str) -> GraphExpansion | None:
        try:
            redis_client = self.redis or default_redis_client()
            if redis_client is None:
                return None
            payload = redis_client.get(graph_expansion_cache_key(symbol))
        except Exception:
            return None
        return graph_expansion_from_json(payload)

    def _load_clickhouse(self, symbol: str) -> GraphExpansion | None:
        try:
            provider = self.clickhouse_provider or default_clickhouse_provider()
            if provider is None:
                return None
            table = os.getenv("AGENT_GRAPH_EXPANSION_CLICKHOUSE_TABLE", DEFAULT_GRAPH_EXPANSION_TABLE)
            rows = provider.query_json_each_row(
                f"""
                SELECT payload
                FROM {provider.table(table)}
                WHERE symbol = {{symbol:String}}
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                {"symbol": symbol},
            )
        except Exception:
            return None
        if not rows:
            return None
        return graph_expansion_from_json(rows[0].get("payload"))


class GraphExpansionWriter:
    def __init__(self, redis_client=None, clickhouse_client=None):
        self.redis = redis_client
        self.clickhouse = clickhouse_client

    def save(self, symbol: str, expansion: GraphExpansion, *, ttl_seconds: int | None = None) -> None:
        normalized_symbol = normalize_symbol(symbol)
        payload = serialize_graph_expansion(expansion)
        ttl = int(ttl_seconds if ttl_seconds is not None else os.getenv("AGENT_GRAPH_EXPANSION_TTL_SECONDS", "21600"))
        redis_client = self.redis or default_redis_client()
        if redis_client is not None and ttl > 0:
            redis_client.setex(graph_expansion_cache_key(normalized_symbol), ttl, payload)
        clickhouse_client = self.clickhouse or default_clickhouse_client()
        if clickhouse_client is not None:
            table = os.getenv("AGENT_GRAPH_EXPANSION_CLICKHOUSE_TABLE", DEFAULT_GRAPH_EXPANSION_TABLE)
            clickhouse_client.insert_json_each_row(table, [graph_expansion_clickhouse_row(normalized_symbol, expansion, payload)])


def refresh_graph_expansions(
    symbols: list[str],
    *,
    provider: GraphDBOntologyProvider | None = None,
    writer: GraphExpansionWriter | None = None,
    relation_version: str | None = None,
) -> list[dict[str, Any]]:
    provider = provider or GraphDBOntologyProvider()
    writer = writer or GraphExpansionWriter()
    version = relation_version or os.getenv("AGENT_GRAPH_EXPANSION_RELATION_VERSION", "v1")
    results = []
    for symbol in unique_symbols(symbols):
        evidence = provider.fetch(ProviderRequest(symbol, "graph expansion refresh"))
        expansion = build_graph_expansion_from_evidence(symbol, evidence, relation_version=version)
        writer.save(symbol, expansion)
        results.append({
            "symbol": symbol,
            "relatedSymbols": len(expansion.related_symbols),
            "themes": len(expansion.themes),
            "warnings": list(expansion.warnings),
        })
    return results


def build_graph_expansion_from_evidence(symbol: str, evidence: list[EvidenceItem], *, relation_version: str = "v1") -> GraphExpansion:
    primary = normalize_symbol(symbol)
    related: dict[str, RelatedSymbol] = {}
    themes: dict[str, RelatedTheme] = {}
    keywords: list[str] = []
    warnings: list[str] = []
    for item in evidence:
        raw = item.raw if isinstance(item.raw, dict) else {}
        relation_type = str(raw.get("relationType") or "unknown")
        if item.status != "available":
            warning = warning_for_relation_type(relation_type)
            if warning and warning not in warnings:
                warnings.append(warning)
            continue

        theme_name = str(raw.get("themeName") or "").strip()
        if theme_name and theme_name not in themes:
            themes[theme_name] = RelatedTheme(
                name=theme_name,
                category=string_or_none(raw.get("themeCategory")),
                score=score_for_relation(relation_type),
                reason=item.summary,
            )
            keywords.append(theme_name)
        theme_category = str(raw.get("themeCategory") or "").strip()
        if theme_category:
            keywords.append(theme_category)

        ticker = normalize_symbol(raw.get("ticker"))
        if ticker and ticker != primary:
            candidate = RelatedSymbol(
                symbol=ticker,
                relation_type=related_symbol_relation_type(relation_type),
                score=score_for_relation(relation_type),
                reason=item.summary,
                source_refs=source_refs(raw, item),
            )
            current = related.get(ticker)
            if current is None or candidate.score > current.score:
                related[ticker] = candidate

    if not related and not themes and not warnings:
        warnings.append("graph_expansion_empty")
    return GraphExpansion(
        source="graphdb-direct",
        cache_hit=False,
        generated_at=utc_now_iso(),
        relation_version=relation_version,
        related_symbols=sorted(related.values(), key=lambda item: item.score, reverse=True),
        themes=sorted(themes.values(), key=lambda item: item.score, reverse=True),
        keywords=unique_strings(keywords)[:12],
        warnings=warnings,
    )


def load_graph_expansion(symbol: str) -> GraphExpansion:
    return GraphExpansionCache().load(symbol)


def graph_expansion_cache_key(symbol: str) -> str:
    prefix = os.getenv("AGENT_GRAPH_EXPANSION_REDIS_PREFIX", DEFAULT_GRAPH_EXPANSION_PREFIX)
    return f"{prefix}:{normalize_symbol(symbol)}"


def graph_expansion_from_json(value: Any) -> GraphExpansion | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            return None
    elif isinstance(value, dict):
        decoded = value
    else:
        return None
    return graph_expansion_from_dict(decoded)


def serialize_graph_expansion(expansion: GraphExpansion) -> str:
    return json.dumps(expansion.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str)


def graph_expansion_clickhouse_row(symbol: str, expansion: GraphExpansion, payload: str | None = None) -> dict[str, Any]:
    return {
        "symbol": normalize_symbol(symbol),
        "relation_version": expansion.relation_version or os.getenv("AGENT_GRAPH_EXPANSION_RELATION_VERSION", "v1"),
        "generated_at": expansion.generated_at or utc_now_iso(),
        "payload": payload or serialize_graph_expansion(expansion),
    }


def graph_expansion_from_dict(value: dict[str, Any]) -> GraphExpansion:
    related_values = value.get("related_symbols") or value.get("relatedSymbols") or []
    theme_values = value.get("themes") or value.get("related_themes") or value.get("relatedThemes") or []
    return GraphExpansion(
        source=str(value.get("source") or "cache"),
        cache_hit=bool(value.get("cache_hit") if "cache_hit" in value else value.get("cacheHit", True)),
        generated_at=string_or_none(value.get("generated_at") or value.get("generatedAt")),
        relation_version=string_or_none(value.get("relation_version") or value.get("relationVersion")),
        related_symbols=[item for item in (related_symbol_from_dict(item) for item in related_values if isinstance(item, dict)) if item],
        themes=[item for item in (related_theme_from_dict(item) for item in theme_values if isinstance(item, dict)) if item],
        keywords=[str(item) for item in (value.get("keywords") or []) if isinstance(item, (str, int, float))],
        warnings=[str(item) for item in (value.get("warnings") or []) if isinstance(item, (str, int, float))],
    )


def related_symbol_from_dict(value: dict[str, Any]) -> RelatedSymbol | None:
    symbol = normalize_symbol(value.get("symbol"))
    if not symbol:
        return None
    return RelatedSymbol(
        symbol=symbol,
        relation_type=str(value.get("relation_type") or value.get("relationType") or "unknown"),
        score=float_or_default(value.get("score"), 0.0),
        reason=str(value.get("reason") or ""),
        source_refs=[str(item) for item in (value.get("source_refs") or value.get("sourceRefs") or []) if isinstance(item, (str, int, float))],
    )


def related_theme_from_dict(value: dict[str, Any]) -> RelatedTheme | None:
    name = str(value.get("name") or "").strip()
    if not name:
        return None
    return RelatedTheme(
        name=name,
        category=string_or_none(value.get("category")),
        score=float_or_default(value.get("score"), 0.0),
        reason=str(value.get("reason") or ""),
    )


def default_redis_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    import redis

    return redis.from_url(redis_url, decode_responses=True)


def default_clickhouse_provider():
    if not os.getenv("CLICKHOUSE_HTTP_URL"):
        return None
    from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

    return ClickHouseMarketDataProvider()


def default_clickhouse_client():
    if not os.getenv("CLICKHOUSE_HTTP_URL"):
        return None
    from alfaka.storage.clickhouse_loader import ClickHouseHttpClient

    return ClickHouseHttpClient(
        os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        os.getenv("CLICKHOUSE_USER", "default"),
        os.getenv("CLICKHOUSE_PASSWORD", ""),
    )


def symbols_from_env() -> list[str]:
    raw = os.getenv("AGENT_GRAPH_EXPANSION_SYMBOLS", "")
    symbols = [item.strip() for item in raw.split(",") if item.strip()]
    path = os.getenv("AGENT_GRAPH_EXPANSION_SYMBOL_FILE", "")
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                symbols.extend(item.strip() for item in handle.read().replace("\n", ",").split(",") if item.strip())
        except Exception:
            pass
    return unique_symbols(symbols)


def unique_symbols(symbols: list[str]) -> list[str]:
    result = []
    for value in symbols:
        symbol = normalize_symbol(value)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def related_symbol_relation_type(relation_type: str) -> str:
    if relation_type in {"theme", "theme-company"}:
        return "same-theme"
    if relation_type in {"control", "theme-control"}:
        return "control"
    if relation_type in {"supplier", "customer", "competitor", "partner", "same-sector"}:
        return relation_type
    return "unknown"


def score_for_relation(relation_type: str) -> float:
    return {
        "supplier": 0.95,
        "customer": 0.95,
        "control": 0.9,
        "competitor": 0.85,
        "partner": 0.82,
        "theme-control": 0.78,
        "theme-company": 0.72,
        "theme": 0.68,
        "same-sector": 0.58,
    }.get(str(relation_type), 0.45)


def warning_for_relation_type(relation_type: str) -> str | None:
    return {
        "graphdb-unavailable": "graph_expansion_unavailable",
        "no-ontology-evidence": "graph_expansion_empty",
        "no-direct-control": "no_direct_control_path",
    }.get(str(relation_type))


def source_refs(raw: dict[str, Any], item: EvidenceItem) -> list[str]:
    refs = []
    for key in ("sourceUrl", "accession"):
        value = str(raw.get(key) or "").strip()
        if value and value not in refs:
            refs.append(value)
    if item.url and item.url not in refs:
        refs.append(item.url)
    return refs


def unique_strings(values: list[str]) -> list[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default
