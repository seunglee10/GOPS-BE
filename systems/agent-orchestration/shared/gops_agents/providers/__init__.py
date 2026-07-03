from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from alfaka.news.relevance import classify_subject_relevance, is_direct_subject, normalize_subject_level, normalize_symbols
from alfaka.storage.news_daily_summary import clickhouse_row_to_daily_summary

from ..contracts import EvidenceItem, utc_now_iso
from .graph_path_cache import GraphPathCache, build_graph_path_cache_from_env
from .news_cache import NewsEvidenceCache, build_news_cache_from_env


@dataclass
class ProviderRequest:
    symbol: str
    intent: str
    symbols: tuple[str, ...] = field(default_factory=tuple)


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
    def __init__(
        self,
        clickhouse_provider=None,
        limit=None,
        days=None,
        direct_fallback: bool | None = None,
        stale_after_seconds: int | None = None,
        publish_fallback: bool | None = None,
        cache: NewsEvidenceCache | None = None,
        redis_provider=None,
    ):
        self.clickhouse_provider = clickhouse_provider
        self.redis_provider = redis_provider
        self.limit = int(limit or os.getenv("AGENT_NEWS_LIMIT", "12"))
        self.days = int(days or os.getenv("AGENT_NEWS_LOOKBACK_DAYS", "7"))
        self.direct_fallback = bool_env("AGENT_NEWS_DIRECT_FALLBACK", False) if direct_fallback is None else direct_fallback
        self.stale_after_seconds = int(stale_after_seconds or os.getenv("AGENT_NEWS_STALE_AFTER_SECONDS", "21600"))
        self.publish_fallback = bool_env("AGENT_NEWS_FALLBACK_PUBLISH_TO_KAFKA", True) if publish_fallback is None else publish_fallback
        self.cache = cache if cache is not None else build_news_cache_from_env()
        self.cache_ttl_seconds = int(os.getenv("AGENT_NEWS_CACHE_TTL_SECONDS", "300"))
        self.no_data_cache_ttl_seconds = int(os.getenv("AGENT_NEWS_NO_DATA_CACHE_TTL_SECONDS", "60"))
        self.prelocalized_enabled = bool_env("AGENT_NEWS_PRELOCALIZED_ENABLED", True)
        self.prelocalized_redis_first = bool_env("AGENT_NEWS_PRELOCALIZED_REDIS_FIRST", True)
        self.locale = os.getenv("AGENT_NEWS_LOCALE", os.getenv("NEWS_INTELLIGENCE_LOCALE", "ko-KR"))
        self.daily_summary_enabled = bool_env("AGENT_NEWS_DAILY_SUMMARY_ENABLED", True)
        self.daily_summary_limit = int(os.getenv("AGENT_NEWS_DAILY_SUMMARY_LIMIT", "5"))

    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        localized = self._fetch_prelocalized(request)
        if localized:
            return self._cache_set(request, localized)

        cached = self._cache_get(request)
        if cached is not None:
            return cached

        clickhouse_error: Exception | None = None
        rows = []
        requested_symbols = self._request_symbols(request)
        try:
            provider = self.clickhouse_provider or self._default_provider()
            for symbol in requested_symbols:
                rows.extend(provider.news_articles(symbol, limit=self.limit, days=self.days))
        except Exception as exc:
            clickhouse_error = exc

        evidence = filter_subject_relevance(normalize_news_evidence([self._row_to_evidence(row, request, source="clickhouse") for row in rows]), limit=self.limit)
        should_fallback = self.direct_fallback and (not evidence or self._is_stale(evidence[0]))
        if should_fallback:
            fallback = self._fetch_alpaca_fallback(request)
            if any(item.status == "available" for item in fallback):
                return self._cache_set(request, normalize_news_evidence([*fallback, *evidence])[: self.limit])
            if not evidence:
                return self._cache_set(request, fallback)

        if clickhouse_error is not None and not evidence:
            return self._cache_set(request, [
                EvidenceItem.no_data(
                    "news",
                    "News provider not available",
                    f"뉴스 ClickHouse provider 미연결 또는 조회 실패: {clickhouse_error}",
                )
            ])
        if not evidence:
            return self._cache_set(request, [
                EvidenceItem.no_data(
                    "news",
                    "No news articles",
                    f"{request.symbol} 관련 저장 뉴스가 없습니다.",
                )
            ])
        return self._cache_set(request, evidence)

    def _fetch_prelocalized(self, request: ProviderRequest) -> list[EvidenceItem]:
        if not self.prelocalized_enabled:
            return []
        rows = []
        requested_symbols = self._request_symbols(request)
        if self.prelocalized_redis_first:
            try:
                redis_provider = self.redis_provider or (self._default_redis_provider() if os.getenv("REDIS_URL") else None)
                if hasattr(redis_provider, "localized_news_articles_for_symbols"):
                    rows.extend(redis_provider.localized_news_articles_for_symbols(requested_symbols, limit=self.limit, locale=self.locale))
            except Exception:
                rows = []
        if not rows:
            try:
                provider = self.clickhouse_provider or self._default_provider()
                if hasattr(provider, "localized_news_articles_for_symbols"):
                    rows.extend(provider.localized_news_articles_for_symbols(requested_symbols, limit=self.limit, days=self.days, locale=self.locale))
                elif len(requested_symbols) == 1 and hasattr(provider, "localized_news_articles"):
                    rows.extend(provider.localized_news_articles(requested_symbols[0], limit=self.limit, days=self.days, locale=self.locale))
            except Exception:
                rows = []
        evidence = normalize_news_evidence([self._row_to_evidence(row, request, source="prelocalized") for row in rows])
        return filter_subject_relevance(evidence, limit=self.limit)

    def fetch_daily_summaries(self, request: ProviderRequest) -> list[dict[str, Any]]:
        if not self.daily_summary_enabled or request.symbols:
            return []
        symbol = self._request_symbols(request)[0]
        rows = []
        if self.prelocalized_redis_first:
            try:
                redis_provider = self.redis_provider or (self._default_redis_provider() if os.getenv("REDIS_URL") else None)
                if hasattr(redis_provider, "company_daily_news_summaries"):
                    rows.extend(redis_provider.company_daily_news_summaries(symbol, limit=self.daily_summary_limit, locale=self.locale))
            except Exception:
                rows = []
        if not rows:
            try:
                provider = self.clickhouse_provider or self._default_provider()
                if hasattr(provider, "company_daily_news_summaries"):
                    rows.extend(provider.company_daily_news_summaries(symbol, limit=self.daily_summary_limit, days=max(self.days, 30), locale=self.locale))
            except Exception:
                rows = []
        return [clickhouse_row_to_daily_summary(row) for row in rows if isinstance(row, dict)]

    def _request_symbols(self, request: ProviderRequest) -> list[str]:
        raw_symbols = request.symbols or (request.symbol,)
        symbols = []
        for value in raw_symbols:
            symbol = str(value or "").strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?", symbol):
                continue
            if symbol not in symbols:
                symbols.append(symbol)
        return symbols or [str(request.symbol or "UNKNOWN").strip().upper() or "UNKNOWN"]

    def _row_to_evidence(self, row: dict, request: ProviderRequest, *, source: str) -> EvidenceItem:
        requested_symbols = self._request_symbols(request)
        row_symbols = normalize_symbols(row.get("symbols"))
        symbol = str(row.get("targetSymbol") or row.get("target_symbol") or row.get("symbol") or "").strip().upper()
        if not symbol and len(row_symbols) == 1 and row_symbols[0] in requested_symbols:
            symbol = row_symbols[0]
        if not symbol:
            symbol = str(requested_symbols[0] if requested_symbols else request.symbol).strip().upper()
        original_title = str(row.get("headline") or row.get("title") or "Untitled news")
        original_summary = str(row.get("summary") or row.get("content") or row.get("headline") or row.get("title") or "뉴스 요약이 없습니다.")
        localized_title = row.get("localizedTitle") or row.get("localizedHeadline") or row.get("localized_title") or row.get("localized_headline")
        localized_summary = row.get("localizedSummary") or row.get("localized_summary") or row.get("localizedSummaryText") or row.get("localized_summary_text")
        title = str(localized_title or original_title)
        summary = str(localized_summary or original_summary)
        published_at = row.get("publishedAt") or row.get("published_at")
        received_at = row.get("receivedAt") or row.get("received_at")
        event_type = row.get("eventType") or row.get("event_type")
        if not event_type or str(event_type).upper() in {"NEWS", "NEWS_ARTICLE"}:
            event_type = classify_news_event_type(f"{title} {summary}")
        impact_direction = row.get("impactDirection") or row.get("impact_direction") or classify_news_impact_direction(f"{title} {summary}")
        relevance = subject_relevance_for_row(symbol, original_title, original_summary, row)
        subject_relevance = normalize_subject_level(row.get("subjectRelevance") or row.get("subject_relevance") or relevance["subjectRelevance"])
        relevance_score_v2 = float_or_default(row.get("relevanceScoreV2") or row.get("relevance_score_v2"), relevance["relevanceScoreV2"])
        relevance_score = score_news_relevance(symbol, original_title, original_summary, row.get("symbols"), subject_relevance=subject_relevance, relevance_score_v2=relevance_score_v2)
        importance_score = score_news_importance(
            symbol=symbol,
            title=title,
            summary=summary,
            source=str(row.get("source") or ""),
            published_at=published_at,
            event_type=event_type,
            impact_direction=impact_direction,
            relevance_score=relevance_score,
            symbols=row.get("symbols"),
        )
        raw = {
            "articleId": row.get("articleId") or row.get("article_id"),
            "source": row.get("source"),
            "author": row.get("author"),
            "headline": title,
            "originalTitle": original_title,
            "originalSummary": original_summary,
            "symbol": row.get("symbol") or symbol,
            "targetSymbol": symbol,
            "symbols": row.get("symbols") if isinstance(row.get("symbols"), list) else [symbol],
            "topic": request.symbol if request.symbol != symbol else None,
            "publishedAt": published_at,
            "receivedAt": received_at,
            "impactDirection": impact_direction,
            "eventType": event_type,
            "sentiment": row.get("sentiment"),
            "keyPoints": row.get("keyPoints") or row.get("key_points"),
            "positivePoints": row.get("positivePoints") or row.get("positive_points"),
            "concerns": row.get("concerns"),
            "whyItMatters": row.get("whyItMatters") or row.get("why_it_matters"),
            "subjectRelevance": subject_relevance,
            "relevanceScoreV2": relevance_score_v2,
            "relevanceReason": row.get("relevanceReason") or row.get("relevance_reason") or relevance["relevanceReason"],
            "directSignals": row.get("directSignals") or row.get("direct_signals") or relevance["directSignals"],
            "isDirectSubject": is_direct_subject(subject_relevance),
            "relevanceScore": relevance_score,
            "importanceScore": importance_score,
            "dataSource": source,
        }
        if localized_title:
            raw["localizedTitle"] = title
        if localized_summary:
            raw["localizedSummary"] = summary
        return EvidenceItem(
            provider="news",
            status="available",
            title=title,
            summary=summary,
            observedAt=str(published_at or received_at or utc_now_iso()),
            url=row.get("url"),
            raw=raw,
        )

    def _fetch_alpaca_fallback(self, request: ProviderRequest) -> list[EvidenceItem]:
        try:
            from alfaka.alpaca.news import build_news_events, fetch_alpaca_news
            from alfaka.common.secrets import load_alpaca_credentials
        except Exception as exc:
            return [EvidenceItem.no_data("news", "Alpaca fallback unavailable", f"Alpaca 뉴스 fallback 모듈을 불러오지 못했습니다: {exc.__class__.__name__}")]

        key_id, secret_key = load_alpaca_credentials()
        if not key_id or not secret_key:
            return [EvidenceItem.no_data("news", "Alpaca fallback credentials missing", "Alpaca 뉴스 fallback에 사용할 API 키가 설정되어 있지 않습니다.")]

        try:
            include_content = bool_env("ALPACA_NEWS_INCLUDE_CONTENT", False)
            requested_symbols = self._request_symbols(request)
            articles = fetch_alpaca_news(
                key_id,
                secret_key,
                symbols=requested_symbols,
                limit=int(os.getenv("AGENT_NEWS_FALLBACK_LIMIT", str(self.limit))),
                include_content=include_content,
            )
            events = [
                event
                for article in articles
                if isinstance(article, dict)
                for event in build_news_events(article, requested_symbols=requested_symbols)
            ]
            if self.publish_fallback:
                self._publish_fallback_events(events)
            return normalize_news_evidence([self._row_to_evidence(event, request, source="alpaca-direct") for event in events])[: self.limit]
        except Exception as exc:
            return [EvidenceItem.no_data("news", "Alpaca fallback failed", f"Alpaca 뉴스 fallback 조회에 실패했습니다: {exc.__class__.__name__}")]

    def _publish_fallback_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            from alfaka.common.kafka_io import create_json_producer

            topic = os.getenv("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1")
            producer = create_json_producer(os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"), "gops-agent-news-fallback")
            for event in events:
                producer.send(topic, key=str(event.get("symbol") or "UNKNOWN"), value=event)
            producer.flush(timeout=float(os.getenv("AGENT_NEWS_FALLBACK_KAFKA_FLUSH_SECONDS", "3")))
            producer.close(timeout=1)
        except Exception:
            return

    def _is_stale(self, item: EvidenceItem) -> bool:
        if self.stale_after_seconds <= 0:
            return False
        observed = parse_time_value(item.raw.get("publishedAt") if isinstance(item.raw, dict) else item.observedAt)
        if observed <= 0:
            return True
        now = datetime.now(timezone.utc).timestamp()
        return now - observed > self.stale_after_seconds

    def _default_provider(self):
        from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

        self.clickhouse_provider = ClickHouseMarketDataProvider()
        return self.clickhouse_provider

    def _default_redis_provider(self):
        from alfaka.serving.redis_provider import RedisMarketDataProvider

        self.redis_provider = RedisMarketDataProvider()
        return self.redis_provider

    def _cache_get(self, request: ProviderRequest) -> list[EvidenceItem] | None:
        try:
            return self.cache.get(
                symbol=self._cache_symbol(request),
                limit=self.limit,
                days=self.days,
                fallback_enabled=self.direct_fallback,
            )
        except Exception:
            return None

    def _cache_set(self, request: ProviderRequest, items: list[EvidenceItem]) -> list[EvidenceItem]:
        ttl_seconds = self.cache_ttl_seconds if any(item.status == "available" for item in items) else self.no_data_cache_ttl_seconds
        try:
            self.cache.set(
                symbol=self._cache_symbol(request),
                limit=self.limit,
                days=self.days,
                fallback_enabled=self.direct_fallback,
                items=items,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            return items
        return items

    def _cache_symbol(self, request: ProviderRequest) -> str:
        symbols = self._request_symbols(request)
        if request.symbols:
            return f"{request.symbol}:{','.join(symbols)}"
        return request.symbol


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
    importance = raw.get("importanceScore")
    relevance_score = float(relevance) if isinstance(relevance, (int, float)) else 0.0
    importance_score = float(importance) if isinstance(importance, (int, float)) else 0.0
    return (
        importance_score,
        parse_time_value(raw.get("publishedAt") or raw.get("receivedAt") or item.observedAt),
        relevance_score,
    )


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


def score_news_relevance(symbol: str, title: str, summary: str, symbols: Any = None, *, subject_relevance: str | None = None, relevance_score_v2: float | None = None) -> float:
    if subject_relevance is not None or relevance_score_v2 is not None:
        level = normalize_subject_level(subject_relevance)
        score = relevance_score_v2 if isinstance(relevance_score_v2, (int, float)) else {
            "primary": 0.95,
            "secondary": 0.75,
            "mention": 0.35,
            "irrelevant": 0.0,
        }.get(level, 0.0)
        return round(max(0.0, min(1.0, float(score))), 2)
    text_upper = f"{title} {summary}".upper()
    text_lower = f"{title} {summary}".lower()
    article_symbols = normalize_symbol_values(symbols)
    score = 0.35
    if symbol and symbol.upper() in article_symbols:
        score += 0.5
    elif symbol and symbol.upper() in text_upper:
        score += 0.45
    if any(term in text_lower for term in ("stock", "shares", "earnings", "revenue", "주가", "실적", "매출")):
        score += 0.15
    return round(min(1.0, score), 2)


def score_news_importance(
    *,
    symbol: str,
    title: str,
    summary: str,
    source: str,
    published_at: Any,
    event_type: str,
    impact_direction: str,
    relevance_score: float,
    symbols: Any,
) -> float:
    score = 0.2 + (max(0.0, min(1.0, float(relevance_score))) * 0.35)
    if event_type in {"earnings", "guidance", "regulation", "mna", "legal"}:
        score += 0.18
    elif event_type in {"analyst", "product", "partnership", "macro"}:
        score += 0.12
    if impact_direction in {"positive", "negative", "mixed"}:
        score += 0.08
    if source.lower() in {"reuters", "bloomberg", "dow jones", "wsj", "cnbc", "marketwatch", "alpaca"}:
        score += 0.07
    article_symbols = normalize_symbol_values(symbols)
    if symbol and symbol.upper() in article_symbols:
        score += 0.08
    if len(article_symbols) >= 3:
        score -= 0.04
    observed = parse_time_value(published_at)
    if observed:
        age_hours = max(0.0, (datetime.now(timezone.utc).timestamp() - observed) / 3600)
        if age_hours <= 6:
            score += 0.12
        elif age_hours <= 24:
            score += 0.08
        elif age_hours <= 72:
            score += 0.04
    if any(term in f"{title} {summary}".lower() for term in ("breaking", "exclusive", "속보", "단독")):
        score += 0.06
    return round(min(1.0, score), 2)


def subject_relevance_for_row(symbol: str, title: str, summary: str, row: dict) -> dict[str, Any]:
    raw = parse_json_dict(row.get("raw"))
    return classify_subject_relevance(
        target_symbol=row.get("targetSymbol") or row.get("target_symbol") or symbol,
        headline=title or raw.get("headline"),
        summary=summary or raw.get("summary"),
        content=row.get("content") or raw.get("content"),
        symbols=row.get("symbols") or raw.get("symbols"),
    )


def filter_subject_relevance(items: list[EvidenceItem], *, limit: int) -> list[EvidenceItem]:
    direct = [item for item in items if is_direct_subject((item.raw or {}).get("subjectRelevance"))]
    if direct:
        return direct[: int(limit)]
    mentions = [item for item in items if normalize_subject_level((item.raw or {}).get("subjectRelevance")) == "mention"]
    return mentions[: min(int(limit), 3)] if mentions else items[: int(limit)]


def parse_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def float_or_default(value: Any, fallback: Any) -> float:
    try:
        return float(value)
    except Exception:
        try:
            return float(fallback)
        except Exception:
            return 0.0


def normalize_symbol_values(value: Any) -> set[str]:
    if isinstance(value, str):
        values = re.split(r"[,\\s]+", value)
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return {str(item).strip().upper() for item in values if str(item).strip()}


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        cache: GraphPathCache | None = None,
        cache_ttl_seconds: int | None = None,
        no_data_cache_ttl_seconds: int | None = None,
    ):
        self.sparql_url = sparql_url or os.getenv("GRAPHDB_SPARQL_URL", "http://localhost:7200/repositories/nasdaq-fibo")
        self.limit = clamp_int(limit or os.getenv("AGENT_ONTOLOGY_LIMIT", "20"), default=20, minimum=1, maximum=200)
        graphdb_timeout = timeout_seconds or os.getenv("GRAPHDB_TIMEOUT_SECONDS")
        if graphdb_timeout is None:
            graphdb_timeout = float(os.getenv("AGENT_GRAPHDB_TIMEOUT_MS", "500")) / 1000
        self.timeout_seconds = float(graphdb_timeout)
        self.sparql_client = sparql_client or GraphDBSparqlClient(self.sparql_url, self.timeout_seconds)
        self.cache = cache if cache is not None else build_graph_path_cache_from_env()
        self.cache_ttl_seconds = int(cache_ttl_seconds or os.getenv("AGENT_GRAPH_PATH_CACHE_TTL_SECONDS", "900"))
        self.no_data_cache_ttl_seconds = int(
            no_data_cache_ttl_seconds or os.getenv("AGENT_GRAPH_PATH_CACHE_NO_DATA_TTL_SECONDS", "120")
        )

    def fetch(self, request: ProviderRequest) -> list[EvidenceItem]:
        requested = ontology_request_symbols(request)
        if not requested:
            return [
                EvidenceItem.no_data(
                    "ontology",
                    "Invalid ontology ticker",
                    f"GraphDB ontology lookup skipped because ticker is invalid: {request.symbol}",
                )
            ]

        intent_themes = tuple(matched_theme_names(request.intent))
        cached = self._cache_get(requested, intent_themes)
        if cached is not None:
            return cached

        primary_symbol = requested[0]
        per_symbol_rows: dict[str, dict[str, list[dict[str, Any]]]] = {}
        theme_matched_rows: list[dict[str, Any]] = []
        try:
            for symbol in requested:
                theme_rows = self._query_rows(themes_by_company_query(symbol, self.limit), "ticker-theme")
                control_rows = self._query_rows(control_relationships_by_company_query(symbol, self.limit), "ticker-control-relationship")
                per_symbol_rows[symbol] = {"theme": theme_rows, "control": control_rows}
            for theme_name in intent_themes:
                theme_matched_rows.extend(self._query_rows(companies_by_theme_query(theme_name, self.limit), "theme-company"))
                theme_matched_rows.extend(self._query_rows(theme_control_relationships_query(theme_name, self.limit), "theme-control-relationship"))
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

        primary_rows = [
            *per_symbol_rows[primary_symbol]["theme"],
            *per_symbol_rows[primary_symbol]["control"],
            *theme_matched_rows,
        ]
        evidence = dedupe_evidence([row_to_ontology_evidence(row) for row in primary_rows])
        if not evidence:
            evidence = [no_ontology_evidence(primary_symbol), no_direct_control_evidence(primary_symbol)]
        elif not per_symbol_rows[primary_symbol]["control"]:
            evidence.append(no_direct_control_evidence(primary_symbol))

        if len(requested) > 1:
            evidence.extend(cross_symbol_relationship_evidence(requested, per_symbol_rows))

        evidence = evidence[: self.limit]
        self._cache_set(requested, intent_themes, evidence)
        return evidence

    def _cache_get(self, symbols: list[str], intent_themes: tuple[str, ...]) -> list[EvidenceItem] | None:
        try:
            return self.cache.get(symbols=tuple(symbols), intent_themes=intent_themes, limit=self.limit)
        except Exception:
            return None

    def _cache_set(self, symbols: list[str], intent_themes: tuple[str, ...], items: list[EvidenceItem]) -> None:
        ttl_seconds = self.cache_ttl_seconds if any(item.status == "available" for item in items) else self.no_data_cache_ttl_seconds
        try:
            self.cache.set(symbols=tuple(symbols), intent_themes=intent_themes, limit=self.limit, items=items, ttl_seconds=ttl_seconds)
        except Exception:
            return None

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


def ontology_request_symbols(request: ProviderRequest) -> list[str]:
    symbols: list[str] = []
    primary = normalize_ticker(request.symbol)
    if primary:
        symbols.append(primary)
    for value in request.symbols:
        ticker = normalize_ticker(str(value or ""))
        if ticker and ticker not in symbols:
            symbols.append(ticker)
    return symbols


def cross_symbol_relationship_evidence(
    symbols: list[str],
    per_symbol_rows: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[EvidenceItem]:
    theme_membership: dict[str, dict[str, str]] = {}
    company_names: dict[str, str] = {}
    for symbol in symbols:
        for row in per_symbol_rows.get(symbol, {}).get("theme", []):
            theme_name = row.get("themeName")
            if theme_name:
                theme_membership.setdefault(symbol, {})[str(theme_name)] = str(row.get("themeCategory") or "")
            if row.get("companyName"):
                company_names[symbol] = str(row["companyName"])

    evidence: list[EvidenceItem] = []
    seen_theme_pairs: set[tuple[str, str, str]] = set()
    for index, symbol_a in enumerate(symbols):
        for symbol_b in symbols[index + 1 :]:
            shared_themes = set(theme_membership.get(symbol_a, {})) & set(theme_membership.get(symbol_b, {}))
            for theme_name in sorted(shared_themes):
                key = (symbol_a, symbol_b, theme_name)
                if key in seen_theme_pairs:
                    continue
                seen_theme_pairs.add(key)
                evidence.append(
                    EvidenceItem(
                        provider="ontology",
                        status="available",
                        title=f"{symbol_a}-{symbol_b} 공통 테마",
                        summary=f"{symbol_a}와 {symbol_b}는 모두 {theme_name} 테마에 속합니다.",
                        observedAt=utc_now_iso(),
                        raw={
                            "type": "cross-symbol-shared-theme",
                            "relationType": "shared-theme",
                            "themeName": theme_name,
                            "symbols": [symbol_a, symbol_b],
                        },
                    )
                )

            evidence.extend(
                cross_symbol_control_evidence(
                    symbol_a,
                    symbol_b,
                    per_symbol_rows.get(symbol_a, {}).get("control", []),
                    per_symbol_rows.get(symbol_b, {}).get("control", []),
                    company_names,
                )
            )

    if not evidence:
        evidence.append(
            EvidenceItem(
                provider="ontology",
                status="no-data",
                title="종목 간 직접 관계 없음",
                summary=f"GraphDB에서 {', '.join(symbols)} 사이의 공유 테마 또는 직접 지배 관계 근거를 찾지 못했습니다.",
                raw={"relationType": "no-shared-relationship", "symbols": list(symbols)},
            )
        )
    return evidence


def cross_symbol_control_evidence(
    symbol_a: str,
    symbol_b: str,
    control_rows_a: list[dict[str, Any]],
    control_rows_b: list[dict[str, Any]],
    company_names: dict[str, str],
) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    company_a = (company_names.get(symbol_a) or symbol_a).strip().lower()
    company_b = (company_names.get(symbol_b) or symbol_b).strip().lower()
    for row in control_rows_a:
        controlled_name = str(row.get("controlledName") or "").strip().lower()
        if controlled_name and (controlled_name == company_b or symbol_b.lower() in controlled_name):
            evidence.append(cross_control_evidence_item(symbol_a, symbol_b, row))
    for row in control_rows_b:
        controlled_name = str(row.get("controlledName") or "").strip().lower()
        if controlled_name and (controlled_name == company_a or symbol_a.lower() in controlled_name):
            evidence.append(cross_control_evidence_item(symbol_b, symbol_a, row))
    return evidence


def cross_control_evidence_item(controller_symbol: str, controlled_symbol: str, row: dict[str, Any]) -> EvidenceItem:
    return EvidenceItem(
        provider="ontology",
        status="available",
        title=f"{controller_symbol}-{controlled_symbol} 직접 지배/자회사 관계",
        summary=f"{controller_symbol}는 {controlled_symbol}({row.get('controlledName') or controlled_symbol})와 직접 지배/자회사 관계 근거가 있습니다.",
        observedAt=utc_now_iso(),
        url=row.get("sourceUrl"),
        raw={
            "type": "cross-symbol-control",
            "relationType": "cross-control",
            "controllerTicker": controller_symbol,
            "controlledTicker": controlled_symbol,
            "controlledName": row.get("controlledName"),
            "confidence": row.get("confidence"),
            "accession": row.get("accession"),
            "sourceUrl": row.get("sourceUrl"),
        },
    )


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
    try:
        from ..query_understanding.topics import extract_theme_names_from_intent

        return extract_theme_names_from_intent(intent)
    except Exception:
        return []


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
