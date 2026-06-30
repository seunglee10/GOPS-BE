from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
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
        return normalize_news_evidence([self._row_to_evidence(row, request.symbol) for row in rows])[: self.limit]

    def _row_to_evidence(self, row: dict, symbol: str) -> EvidenceItem:
        title = str(row.get("headline") or row.get("title") or "Untitled news")
        summary = str(row.get("summary") or row.get("content") or row.get("headline") or row.get("title") or "뉴스 요약이 없습니다.")
        published_at = row.get("publishedAt") or row.get("published_at")
        received_at = row.get("receivedAt") or row.get("received_at")
        event_type = classify_news_event_type(f"{title} {summary}")
        impact_direction = classify_news_impact_direction(f"{title} {summary}")
        return EvidenceItem(
            provider="news",
            status="available",
            title=title,
            summary=summary,
            observedAt=str(published_at or received_at or utc_now_iso()),
            url=row.get("url"),
            raw={
                "articleId": row.get("articleId") or row.get("article_id"),
                "source": row.get("source"),
                "author": row.get("author"),
                "headline": title,
                "publishedAt": published_at,
                "receivedAt": received_at,
                "impactDirection": impact_direction,
                "eventType": event_type,
                "relevanceScore": score_news_relevance(symbol, title, summary),
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


def normalize_news_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    deduped_by_key: dict[tuple[str, str], EvidenceItem] = {}
    for item in items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        article_id = str(raw.get("articleId") or "").strip()
        title = item.title.strip().lower()
        url = str(item.url or "").strip().lower()
        key = ("articleId", article_id) if article_id else ("title-url", f"{title}|{url}")
        current = deduped_by_key.get(key)
        if current is None or news_sort_key(item) > news_sort_key(current):
            deduped_by_key[key] = item
    return sorted(deduped_by_key.values(), key=news_sort_key, reverse=True)


def news_sort_key(item: EvidenceItem) -> tuple[float, float]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    relevance = raw.get("relevanceScore")
    relevance_score = float(relevance) if isinstance(relevance, (int, float)) else 0.0
    return (parse_time_value(raw.get("publishedAt") or raw.get("receivedAt") or item.observedAt), relevance_score)


def parse_time_value(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def classify_news_event_type(text: str) -> str:
    lowered = text.lower()
    keyword_groups = [
        ("earnings", ("earnings", "eps", "revenue", "profit", "실적", "매출", "순이익")),
        ("guidance", ("guidance", "forecast", "outlook", "전망", "가이던스")),
        ("product", ("launch", "product", "chip", "gpu", "ai", "제품", "출시")),
        ("regulation", ("regulation", "regulator", "sec", "probe", "export control", "규제", "조사")),
        ("analyst", ("analyst", "upgrade", "downgrade", "price target", "투자의견", "목표가")),
        ("macro", ("fed", "rate", "inflation", "cpi", "fomc", "금리", "물가", "연준")),
        ("mna", ("acquisition", "merger", "takeover", "인수", "합병")),
        ("legal", ("lawsuit", "court", "settlement", "소송", "법원")),
        ("partnership", ("partnership", "partner", "collaboration", "제휴", "협력")),
    ]
    for event_type, keywords in keyword_groups:
        if any(keyword in lowered for keyword in keywords):
            return event_type
    return "other"


def classify_news_impact_direction(text: str) -> str:
    lowered = text.lower()
    positive_terms = (
        "beat",
        "surge",
        "rise",
        "gain",
        "upgrade",
        "strong",
        "record",
        "growth",
        "호조",
        "상향",
        "강세",
        "증가",
        "성장",
    )
    negative_terms = (
        "miss",
        "fall",
        "drop",
        "downgrade",
        "weak",
        "lawsuit",
        "probe",
        "decline",
        "하향",
        "약세",
        "감소",
        "소송",
        "조사",
    )
    has_positive = any(term in lowered for term in positive_terms)
    has_negative = any(term in lowered for term in negative_terms)
    if has_positive and has_negative:
        return "mixed"
    if has_positive:
        return "positive"
    if has_negative:
        return "negative"
    return "unknown"


def score_news_relevance(symbol: str, title: str, summary: str) -> float:
    text_upper = f"{title} {summary}".upper()
    text_lower = f"{title} {summary}".lower()
    score = 0.35
    if symbol and symbol.upper() in text_upper:
        score += 0.45
    if any(term in text_lower for term in ("stock", "shares", "earnings", "revenue", "주가", "실적", "매출")):
        score += 0.15
    return round(min(1.0, score), 2)


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
            theme_rows = self._query_rows(themes_by_company_query(symbol, self.limit), "ticker-theme")
            control_rows = self._query_rows(control_relationships_by_company_query(symbol, self.limit), "ticker-control-relationship")
            rows = [*theme_rows, *control_rows]
            for theme_name in matched_theme_names(request.intent):
                rows.extend(self._query_rows(companies_by_theme_query(theme_name, self.limit), "theme-company"))
                rows.extend(self._query_rows(theme_control_relationships_query(theme_name, self.limit), "theme-control-relationship"))
        except Exception as exc:
            return [
                EvidenceItem(
                    provider="ontology",
                    status="no-data",
                    title="GraphDB 연결 실패",
                    summary=f"GraphDB 온톨로지 조회에 실패했습니다: {exc.__class__.__name__}",
                    raw={"relationType": "graphdb-unavailable", "errorType": exc.__class__.__name__},
                )
            ]

        evidence = dedupe_evidence([row_to_ontology_evidence(row) for row in rows])
        if not evidence:
            return [no_ontology_evidence(symbol), no_direct_control_evidence(symbol)][: self.limit]
        if not control_rows:
            evidence.append(no_direct_control_evidence(symbol))
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
    relation_type = ontology_relation_type(row_type)
    ticker = str(row.get("ticker") or "").upper()
    company_name = str(row.get("companyName") or ticker or "Unknown company")
    theme_name = row.get("themeName")
    controlled_name = row.get("controlledName")
    confidence = row.get("confidence")
    source_url = row.get("sourceUrl")

    if row_type in {"ticker-control-relationship", "theme-control-relationship"}:
        title = f"{ticker} 직접 지배/자회사 관계" if ticker else "직접 지배/자회사 관계"
        summary = f"{company_name}는 {controlled_name or '확인된 기업'}와 직접 지배/자회사 관계 근거가 있습니다."
        if confidence:
            summary = f"{summary} 신뢰도는 {confidence}입니다."
    elif row_type == "theme-company":
        title = f"{theme_name or '테마'} 관련 기업"
        summary = f"{ticker or company_name}는 {theme_name or '요청된'} 테마에 포함된 기업입니다."
    else:
        title = f"{ticker or company_name} 테마 관계"
        summary = f"{company_name}는 {theme_name or '확인되지 않은'} 테마에 매핑되어 있습니다."

    return EvidenceItem(
        provider="ontology",
        status="available",
        title=title,
        summary=summary,
        observedAt=utc_now_iso(),
        url=source_url,
        raw={
            "type": row_type,
            "relationType": relation_type,
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


def ontology_relation_type(row_type: str) -> str:
    mapping = {
        "ticker-theme": "theme",
        "ticker-control-relationship": "control",
        "theme-company": "theme-company",
        "theme-control-relationship": "theme-control",
    }
    return mapping.get(row_type, "ontology")


def no_direct_control_evidence(symbol: str) -> EvidenceItem:
    return EvidenceItem(
        provider="ontology",
        status="no-data",
        title="직접 지배/자회사 관계 없음",
        summary=f"GraphDB에서 {symbol}의 직접 지배/자회사 관계 근거는 확인되지 않았습니다.",
        raw={"relationType": "no-direct-control", "ticker": symbol},
    )


def no_ontology_evidence(symbol: str) -> EvidenceItem:
    return EvidenceItem(
        provider="ontology",
        status="no-data",
        title="온톨로지 관계 근거 없음",
        summary=f"GraphDB에서 {symbol} 관련 온톨로지 관계 근거를 찾지 못했습니다.",
        raw={"relationType": "no-ontology-evidence", "ticker": symbol},
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
