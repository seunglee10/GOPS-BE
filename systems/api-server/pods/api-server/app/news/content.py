"""뉴스 행(Redis·ClickHouse)을 D13 응답 필드로 바꾸는 순수 함수 모음."""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.instrument_names import name_ko
from market_data.news.categorize import categorize_news, normalize_news_category


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def row_symbols(row: dict[str, Any]) -> list[str]:
    """기사에 붙은 종목 목록. symbols 배열이 없으면 target/symbol 한 개로 대신합니다."""
    values = row.get("symbols")
    if not isinstance(values, list):
        values = _raw(row).get("symbols")
    symbols: list[str] = []
    for value in values if isinstance(values, list) else []:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        for key in ("targetSymbol", "target_symbol", "symbol"):
            symbol = str(row.get(key) or "").strip().upper()
            if symbol and symbol != "UNKNOWN":
                symbols.append(symbol)
                break
    return symbols


def news_category(row: dict[str, Any]) -> str:
    """저장된 category 를 쓰고, 비어 있으면(0002 이전 행) 같은 키워드 규칙으로 계산합니다.

    원문 제목을 먼저 봅니다. 규칙이 영어 기사 기준으로 만들어졌고 번역 제목은 표현이 흔들리기 때문입니다.
    """
    stored = normalize_news_category(row.get("category")) or normalize_news_category(_raw(row).get("category"))
    if stored:
        return stored
    headline = _text(row.get("headline")) or _text(row.get("localizedHeadline")) or _text(row.get("title"))
    summary = _text(row.get("summary")) or _text(row.get("localizedSummary"))
    return categorize_news(headline, summary, row_symbols(row))


def related_symbols(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"symbol": symbol, "nameKo": name_ko(symbol)} for symbol in row_symbols(row)]


def title_ko(row: dict[str, Any]) -> str:
    return (
        _text(row.get("localizedTitle"))
        or _text(row.get("localizedHeadline"))
        or _text(row.get("localized_headline"))
        or _text(row.get("title"))
        or _text(row.get("headline"))
        or "Untitled news"
    )


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n|\r?\n")
_HTML_TAG = re.compile(r"<[^>]+>")


def body_paragraphs(row: dict[str, Any]) -> list[str]:
    """본문 문단.

    news_article_localizations 에는 기사 전문 번역이 없고 한국어 요약(localized_summary, 500자 이내)과
    핵심 포인트(key_points)만 있습니다. 그래서 요약을 첫 문단으로, 요약과 겹치지 않는 핵심 포인트를
    다음 문단으로 씁니다. 현지화 행이 없으면 원문 요약/본문으로 대신합니다.
    """
    paragraphs: list[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if not text:
            return
        for part in _PARAGRAPH_SPLIT.split(_HTML_TAG.sub(" ", text)):
            cleaned = " ".join(part.split())
            if not cleaned:
                continue
            if any(cleaned in existing or existing in cleaned for existing in paragraphs):
                continue
            paragraphs.append(cleaned)

    add(row.get("localizedSummary") or row.get("localized_summary"))
    key_points = row.get("keyPoints") or row.get("key_points")
    for point in key_points if isinstance(key_points, list) else []:
        add(point)
    if not paragraphs:
        add(row.get("summary"))
        add(row.get("content"))
    return paragraphs
