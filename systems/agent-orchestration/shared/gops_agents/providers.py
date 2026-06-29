from __future__ import annotations

import os
from dataclasses import dataclass

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
