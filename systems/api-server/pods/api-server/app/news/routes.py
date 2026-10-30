"""D13 기사 본문 API. /api/market/ 아래가 아니라 /api/news 에 둡니다(목업 pageNews)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request

from app.news.content import body_paragraphs, news_category, related_symbols, title_ko
from app.news.repository import news_repository_from_app

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("/{article_id}")
def news_article(
    request: Request,
    article_id: str = Path(min_length=1, max_length=128),
    locale: str = Query(default="ko-KR", max_length=16),
) -> dict[str, Any]:
    repository = news_repository_from_app(request.app)
    try:
        row = repository.localized_article(article_id, locale) or repository.original_article(article_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "news_unavailable"}) from exc
    if not row:
        raise HTTPException(status_code=404, detail={"code": "news_not_found"})
    return {
        "id": str(row.get("articleId") or row.get("article_id") or article_id),
        "titleKo": title_ko(row),
        "source": row.get("source"),
        "publishedAt": row.get("publishedAt") or row.get("published_at"),
        "url": row.get("url"),
        "category": news_category(row),
        "relatedSymbols": related_symbols(row),
        "bodyKo": body_paragraphs(row),
    }
