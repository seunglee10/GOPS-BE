# 역할: Alpaca News API 응답을 GOPS 내부 뉴스 이벤트 계약으로 정규화합니다.
# 사용: alpaca-news-ingestor가 Kafka topic market.news.alpaca.v1에 넣을 payload를 만듭니다.
import hashlib
import os
import time

from market_data.common.env import parse_csv, utc_now_iso


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
    page_token=None,
):
    page = fetch_alpaca_news_page(
        key_id,
        secret_key,
        symbols=symbols,
        limit=limit,
        include_content=include_content,
        start=start,
        end=end,
        sort=sort,
        page_token=page_token,
    )
    return page["news"]


def iter_alpaca_news_pages(
    key_id,
    secret_key,
    symbols=None,
    limit=50,
    include_content=False,
    start=None,
    end=None,
    sort="asc",
    page_token=None,
    max_pages=None,
):
    next_page_token = page_token
    page_number = 0
    while True:
        page = fetch_alpaca_news_page(
            key_id,
            secret_key,
            symbols=symbols,
            limit=limit,
            include_content=include_content,
            start=start,
            end=end,
            sort=sort,
            page_token=next_page_token,
        )
        page_number += 1
        yield {**page, "pageNumber": page_number}
        next_page_token = page.get("nextPageToken")
        if not next_page_token:
            return
        if max_pages and page_number >= int(max_pages):
            return


def fetch_alpaca_news_page(
    key_id,
    secret_key,
    symbols=None,
    limit=50,
    include_content=False,
    start=None,
    end=None,
    sort="desc",
    page_token=None,
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
    if page_token:
        params["page_token"] = page_token

    max_attempts = max(1, int(os.getenv("ALPACA_NEWS_MAX_RETRIES", "5")))
    retry_sleep_seconds = max(0.0, float(os.getenv("ALPACA_NEWS_RETRY_SLEEP_SECONDS", "1")))
    retry_max_sleep_seconds = max(retry_sleep_seconds, float(os.getenv("ALPACA_NEWS_RETRY_MAX_SLEEP_SECONDS", "30")))
    response = None
    endpoint = os.getenv("ALPACA_NEWS_API_URL", NEWS_API_URL)
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret_key,
    }
    request_exception = getattr(requests, "RequestException", Exception)
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(
                endpoint,
                headers=headers,
                params=params,
                timeout=float(os.getenv("ALPACA_NEWS_TIMEOUT_SECONDS", "10")),
            )
        except request_exception as exc:
            if attempt >= max_attempts:
                raise RuntimeError(f"Alpaca news request failed after retries: {exc}") from exc
            time.sleep(news_retry_delay(None, attempt, retry_sleep_seconds, retry_max_sleep_seconds))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt >= max_attempts:
                raise RuntimeError(f"Alpaca news request failed: status={response.status_code}, body={response.text}")
            time.sleep(news_retry_delay(response, attempt, retry_sleep_seconds, retry_max_sleep_seconds))
            continue
        break
    if response is None:
        return {"news": [], "nextPageToken": None}
    if response.status_code >= 400:
        raise RuntimeError(f"Alpaca news request failed: status={response.status_code}, body={response.text}")
    payload = response.json()
    news = payload.get("news") if isinstance(payload, dict) and isinstance(payload.get("news"), list) else []
    next_page_token = payload.get("next_page_token") if isinstance(payload, dict) else None
    return {"news": news, "nextPageToken": next_page_token}


def news_retry_delay(response, attempt, base_seconds, max_seconds):
    retry_after = None
    if response is not None:
        retry_after = getattr(response, "headers", {}).get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), max_seconds)
        except ValueError:
            pass
    return min(base_seconds * (2 ** max(attempt - 1, 0)), max_seconds)


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
