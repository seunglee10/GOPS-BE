from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .contracts import EvidenceItem, utc_now_iso


@dataclass
class ProviderRequest:
    symbol: str
    intent: str


class NewsProvider:
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        raise NotImplementedError


class MacroProvider:
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        raise NotImplementedError


class OntologyProvider:
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        raise NotImplementedError


class EmptyNewsProvider(NewsProvider):
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        return [
            EvidenceItem.no_data(
                "news",
                "News provider not configured",
                f"No external news source is configured for {request.symbol} in v1.",
            )
        ]


class ClickHouseNewsProvider(NewsProvider):
    def __init__(self, clickhouse_provider=None, limit=None, days=None):
        self.clickhouse_provider = clickhouse_provider
        self.limit = int(limit or os.getenv("AGENT_NEWS_LIMIT", "8"))
        self.days = int(days or os.getenv("AGENT_NEWS_LOOKBACK_DAYS", "7"))

    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        try:
            provider = self.clickhouse_provider or self._default_provider()
            rows = provider.news_articles(request.symbol, limit=self.limit, days=self.days)
        except Exception as exc:
            return [
                EvidenceItem.no_data(
                    "news",
                    "News provider not available",
                    f"뉴스 ClickHouse provider 미연결 또는 조회 실패: {exc}",
                )
            ]
        if not rows:
            return [
                EvidenceItem.no_data(
                    "news",
                    "No news articles",
                    f"{request.symbol} 관련 Alpaca 뉴스가 아직 저장되어 있지 않습니다.",
                )
            ]
        return [self._row_to_evidence(row) for row in rows]

    def _row_to_evidence(self, row: dict) -> EvidenceItem:
        return EvidenceItem(
            provider="news",
            status="available",
            title=str(row.get("headline") or "Untitled news"),
            summary=str(row.get("summary") or row.get("headline") or "뉴스 요약이 없습니다."),
            observedAt=str(row.get("publishedAt") or row.get("receivedAt") or utc_now_iso()),
            url=row.get("url"),
            raw={
                "articleId": row.get("articleId"),
                "source": row.get("source"),
                "author": row.get("author"),
                "publishedAt": row.get("publishedAt"),
                "receivedAt": row.get("receivedAt"),
            },
        )

    def _default_provider(self):
        from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

        self.clickhouse_provider = ClickHouseMarketDataProvider()
        return self.clickhouse_provider


class EmptyMacroProvider(MacroProvider):
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        return [
            EvidenceItem.no_data(
                "macro",
                "Macro provider not configured",
                "No external macro data source is configured in v1.",
            )
        ]


class EmptyOntologyProvider(OntologyProvider):
    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        return [
            EvidenceItem.no_data(
                "ontology",
                "Ontology provider not configured",
                f"No company relationship graph is configured for {request.symbol} in v1.",
            )
        ]


GRAPHDB_THEME_NAMES = (
    "AI/반도체/데이터센터",
    "클라우드/소프트웨어/사이버보안",
    "인터넷 플랫폼/미디어/광고",
    "소비/리테일/여행",
    "결제/핀테크/거래소",
    "은행/보험/자산운용",
    "헬스케어/제약/의료기기",
    "에너지/전력/전기화",
    "산업재/방산/운송/인프라",
    "부동산/REIT/통신 인프라",
    "통신/네트워크",
    "자동차/모빌리티",
    "필수소비재",
    "소재/화학",
)


class GraphDBSparqlClient:
    def __init__(self, sparql_url: str, timeout_seconds: float = 5.0):
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


class GraphDBOntologyProvider(OntologyProvider):
    def __init__(
        self,
        sparql_client: GraphDBSparqlClient | None = None,
        sparql_url: str | None = None,
        limit: int | None = None,
        timeout_seconds: float | None = None,
    ):
        self.sparql_url = sparql_url or os.getenv("GRAPHDB_SPARQL_URL", "http://localhost:7200/repositories/nasdaq-fibo")
        self.limit = clamp_int(limit or os.getenv("AGENT_ONTOLOGY_LIMIT", "20"), default=20, minimum=1, maximum=200)
        self.timeout_seconds = float(timeout_seconds or os.getenv("GRAPHDB_TIMEOUT_SECONDS", "5"))
        self.sparql_client = sparql_client or GraphDBSparqlClient(self.sparql_url, self.timeout_seconds)

    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        symbol = normalize_ticker(request.symbol)
        if not symbol:
            return [
                EvidenceItem.no_data(
                    "ontology",
                    "Invalid ontology ticker",
                    f"GraphDB ontology lookup skipped because ticker is invalid: {request.symbol}",
                )
            ]

        try:
            rows = []
            rows.extend(self._query_rows(themes_by_company_query(symbol, self.limit), "ticker-theme"))
            rows.extend(self._query_rows(control_relationships_by_company_query(symbol, self.limit), "ticker-control-relationship"))
            for theme_name in matched_theme_names(request.intent):
                rows.extend(self._query_rows(companies_by_theme_query(theme_name, self.limit), "theme-company"))
                rows.extend(self._query_rows(theme_control_relationships_query(theme_name, self.limit), "theme-control-relationship"))
        except Exception as exc:
            return [
                EvidenceItem.no_data(
                    "ontology",
                    "GraphDB ontology provider unavailable",
                    f"GraphDB ontology 조회 실패 또는 미연결: {exc.__class__.__name__}",
                )
            ]

        evidence = dedupe_evidence([row_to_ontology_evidence(row) for row in rows])
        if not evidence:
            return [
                EvidenceItem.no_data(
                    "ontology",
                    "No ontology evidence",
                    f"{symbol} 관련 GraphDB 온톨로지 관계 근거를 찾지 못했습니다.",
                )
            ]
        return evidence[: self.limit]

    def _query_rows(self, sparql: str, row_type: str) -> list[dict[str, Any]]:
        payload = self.sparql_client.query(sparql)
        return [{**row, "type": row_type} for row in sparql_json_to_rows(payload)]


def sparql_json_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = payload.get("results", {}).get("bindings", [])
    rows: list[dict[str, Any]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        rows.append({
            key: value.get("value")
            for key, value in binding.items()
            if isinstance(value, dict) and value.get("value") is not None
        })
    return rows


def row_to_ontology_evidence(row: dict[str, Any]) -> EvidenceItem:
    row_type = str(row.get("type") or "ontology")
    ticker = str(row.get("ticker") or "").upper()
    company_name = str(row.get("companyName") or ticker or "Unknown company")
    theme_name = row.get("themeName")
    controlled_name = row.get("controlledName")
    confidence = row.get("confidence")
    source_url = row.get("sourceUrl")

    if row_type in {"ticker-control-relationship", "theme-control-relationship"}:
        title = f"{ticker} control relationship" if ticker else "Control relationship"
        summary = f"{company_name} has a derived control relationship with {controlled_name or 'an entity'}."
        if confidence:
            summary = f"{summary} confidence={confidence}."
    elif row_type == "theme-company":
        title = f"{theme_name or 'Theme'} company"
        summary = f"{ticker or company_name} is included in the {theme_name or 'requested'} investment theme."
    else:
        title = f"{ticker or company_name} ontology theme"
        summary = f"{company_name} is mapped to theme {theme_name or 'unknown'}."

    return EvidenceItem(
        provider="ontology",
        status="available",
        title=title,
        summary=summary,
        observedAt=utc_now_iso(),
        url=source_url,
        raw={
            "type": row_type,
            "ticker": ticker or None,
            "companyName": row.get("companyName"),
            "themeName": theme_name,
            "themeCategory": row.get("themeCategory"),
            "sector": row.get("sector"),
            "controlledName": controlled_name,
            "confidence": confidence,
            "accession": row.get("accession"),
            "sourceUrl": source_url,
        },
    )


def normalize_ticker(value: str) -> str | None:
    ticker = str(value or "").strip().upper()
    if not re.match(r"^[A-Z0-9.\-]{1,16}$", ticker):
        return None
    return ticker


def matched_theme_names(intent: str) -> list[str]:
    text = str(intent or "")
    return [theme_name for theme_name in GRAPHDB_THEME_NAMES if theme_name in text]


def dedupe_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen = set()
    deduped = []
    for item in items:
        key = (item.provider, item.title, item.summary, item.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def sparql_string_literal(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def themes_by_company_query(ticker: str, limit: int) -> str:
    return f"""
PREFIX gops: <urn:gops:ontology:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?ticker ?companyName ?theme ?themeName ?themeCategory
WHERE {{
  ?company gops:ticker ?ticker ;
           rdfs:label ?companyName .
  FILTER (UCASE(STR(?ticker)) = {sparql_string_literal(ticker)})
  GRAPH <urn:gops:graph:themes:current> {{
    ?company gops:hasTheme ?theme .
    ?theme gops:themeNameKo ?themeName .
    OPTIONAL {{ ?theme gops:themeCategory ?themeCategory . }}
  }}
}}
ORDER BY ?themeName
LIMIT {clamp_int(limit, default=20, minimum=1, maximum=200)}
""".strip()


def control_relationships_by_company_query(ticker: str, limit: int) -> str:
    return f"""
PREFIX gops: <urn:gops:ontology:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?ticker ?companyName ?controlledName ?confidence ?accession ?sourceUrl
WHERE {{
  ?company gops:ticker ?ticker ;
           rdfs:label ?companyName .
  FILTER (UCASE(STR(?ticker)) = {sparql_string_literal(ticker)})
  GRAPH <urn:gops:graph:derived:current> {{
    ?relationship a gops:DerivedControlRelationship ;
                  gops:subjectEntity ?company ;
                  gops:objectEntity ?controlled ;
                  gops:confidence ?confidence ;
                  gops:accessionNumber ?accession ;
                  gops:sourceUrl ?sourceUrl .
    OPTIONAL {{ ?controlled rdfs:label ?controlledName . }}
  }}
}}
ORDER BY ?controlledName
LIMIT {clamp_int(limit, default=20, minimum=1, maximum=200)}
""".strip()


def companies_by_theme_query(theme_name: str, limit: int) -> str:
    return f"""
PREFIX gops: <urn:gops:ontology:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?ticker ?company ?companyName ?sector ?themeName
WHERE {{
  GRAPH <urn:gops:graph:themes:current> {{
    ?company gops:hasTheme ?theme .
    ?theme gops:themeNameKo ?themeName .
  }}
  ?company gops:ticker ?ticker ;
           rdfs:label ?companyName .
  OPTIONAL {{ ?company gops:sector ?sector . }}
  FILTER (STR(?themeName) = {sparql_string_literal(theme_name)})
}}
ORDER BY ?ticker
LIMIT {clamp_int(limit, default=20, minimum=1, maximum=200)}
""".strip()


def theme_control_relationships_query(theme_name: str, limit: int) -> str:
    return f"""
PREFIX gops: <urn:gops:ontology:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?ticker ?companyName ?controlledName ?confidence ?accession ?sourceUrl
WHERE {{
  GRAPH <urn:gops:graph:themes:current> {{
    ?company gops:hasTheme ?theme .
    ?theme gops:themeNameKo ?themeName .
  }}
  ?company gops:ticker ?ticker ;
           rdfs:label ?companyName .
  GRAPH <urn:gops:graph:derived:current> {{
    ?relationship a gops:DerivedControlRelationship ;
                  gops:subjectEntity ?company ;
                  gops:objectEntity ?controlled ;
                  gops:confidence ?confidence ;
                  gops:accessionNumber ?accession ;
                  gops:sourceUrl ?sourceUrl .
    OPTIONAL {{ ?controlled rdfs:label ?controlledName . }}
  }}
  FILTER (STR(?themeName) = {sparql_string_literal(theme_name)})
}}
ORDER BY ?ticker ?controlledName
LIMIT {clamp_int(limit, default=20, minimum=1, maximum=200)}
""".strip()
