# 역할: Alpaca News API 응답을 GOPS 내부 뉴스 이벤트 계약으로 정규화합니다.
# 사용: alpaca-news-ingestor가 Kafka topic market.news.alpaca.v1에 넣을 payload를 만듭니다.
import hashlib
import os

from alfaka.common.env import parse_csv, utc_now_iso


NEWS_API_URL = "https://data.alpaca.markets/v1beta1/news"


def fetch_alpaca_news(
    key_id,
    secret_key,
    symbols=None,
    limit=50,
    include_content=False,
    start=None,
    end=None,
    sort="desc",
):
    import requests

    params = {
        "limit": int(limit),
        "sort": sort,
    }
    normalized_symbols = normalize_symbol_list(symbols or [])
    if normalized_symbols:
        params["symbols"] = ",".join(normalized_symbols)
    if include_content:
        params["include_content"] = "true"
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    response = requests.get(
        os.getenv("ALPACA_NEWS_API_URL", NEWS_API_URL),
        headers={
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        },
        params=params,
        timeout=float(os.getenv("ALPACA_NEWS_TIMEOUT_SECONDS", "10")),
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("news") if isinstance(payload, dict) and isinstance(payload.get("news"), list) else []


def build_news_events(article, requested_symbols=None, received_at=None):
    symbols = normalize_article_symbols(article)
    requested = set(normalize_symbol_list(requested_symbols or []))
    if requested and symbols:
        symbols = [symbol for symbol in symbols if symbol in requested] or symbols
    if not symbols:
        symbols = normalize_symbol_list(requested_symbols or [])[:1] or ["UNKNOWN"]
    return [build_news_event(article, symbol=symbol, received_at=received_at) for symbol in symbols]


def build_news_event(article, symbol=None, received_at=None):
    received_at = received_at or utc_now_iso()
    article_id = article_identifier(article)
    normalized_symbol = normalize_symbol(symbol or first_symbol(article) or "UNKNOWN")
    created_at = article.get("created_at") or article.get("createdAt") or article.get("published_at") or received_at
    updated_at = article.get("updated_at") or article.get("updatedAt")
    source_event_id = f"alpaca/news/{normalized_symbol}/{article_id}"
    return {
        "eventType": "NEWS_ARTICLE",
        "symbol": normalized_symbol,
        "articleId": article_id,
        "headline": str(article.get("headline") or article.get("title") or "Untitled news"),
        "summary": article.get("summary"),
        "content": article.get("content"),
        "url": article.get("url"),
        "source": article.get("source") or "alpaca",
        "author": article.get("author"),
        "publishedAt": created_at,
        "updatedAt": updated_at,
        "receivedAt": received_at,
        "symbols": normalize_article_symbols(article),
        "sourceEventId": source_event_id,
        "raw": article,
    }


def normalize_article_symbols(article):
    symbols = article.get("symbols")
    if isinstance(symbols, str):
        symbols = parse_csv(symbols)
    if not isinstance(symbols, list):
        symbols = []
    return normalize_symbol_list(symbols)


def normalize_symbol_list(symbols):
    normalized = []
    seen = set()
    for value in symbols:
        symbol = normalize_symbol(value)
        if symbol and symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def normalize_symbol(value):
    return str(value or "").strip().upper()


def first_symbol(article):
    symbols = normalize_article_symbols(article)
    return symbols[0] if symbols else None


def article_identifier(article):
    value = article.get("id") or article.get("articleId")
    if value is not None and str(value).strip():
        return str(value)
    basis = "|".join(str(article.get(key) or "") for key in ("headline", "url", "created_at", "updated_at"))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]
