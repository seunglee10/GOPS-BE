"""기사 단건 조회 저장소. 라우트는 app.state.news_repository 로 주입받아 테스트에서 바꿔 끼웁니다."""

from __future__ import annotations

from typing import Any, Protocol


class NewsRepository(Protocol):
    def localized_article(self, article_id: str, locale: str) -> dict[str, Any] | None: ...

    def original_article(self, article_id: str) -> dict[str, Any] | None: ...


class ClickHouseNewsRepository:
    """news_article_localizations 를 먼저 보고, 없으면 news_articles 원문 행을 돌려줍니다.

    두 테이블 모두 article_id 가 정렬 키 맨 앞이 아니라 파티션 전체를 훑습니다. 기사 화면은
    목록에서 한 건씩 들어오는 호출이라 지금 규모에서는 문제없고, 느려지면 article_id 에
    bloom_filter 인덱스를 다는 마이그레이션을 추가합니다.
    """

    def __init__(self, clickhouse_provider: Any) -> None:
        self._clickhouse = clickhouse_provider

    def localized_article(self, article_id: str, locale: str) -> dict[str, Any] | None:
        method = getattr(self._clickhouse, "localized_news_article_by_id", None)
        return method(article_id, locale=locale) if callable(method) else None

    def original_article(self, article_id: str) -> dict[str, Any] | None:
        method = getattr(self._clickhouse, "news_article_by_id", None)
        return method(article_id) if callable(method) else None


def news_repository_from_app(app: Any) -> NewsRepository:
    existing = getattr(app.state, "news_repository", None)
    if existing is not None:
        return existing
    from app.services.alfaka_market_data import get_market_data_provider

    provider = get_market_data_provider()
    repository = ClickHouseNewsRepository(getattr(provider, "clickhouse_provider", None))
    app.state.news_repository = repository
    return repository
