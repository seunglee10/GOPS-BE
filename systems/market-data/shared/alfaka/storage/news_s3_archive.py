"""S3 canonical raw archive helpers for Alpaca news articles."""

import json
import re
from datetime import datetime, timezone

from alfaka.alpaca.news import article_identifier, normalize_article_symbols, normalize_symbol
from alfaka.news.relevance import classify_subject_relevance


def upload_canonical_news_article_to_s3(s3, bucket, prefix, article, *, force=False, received_at=None):
    article_id = article_identifier(article)
    key = canonical_news_article_key(prefix, article_id)
    if not force and s3_object_exists(s3, bucket, key):
        return {"key": key, "articleId": article_id, "stored": False}

    body = json.dumps(
        {
            "schemaVersion": 1,
            "source": "alpaca",
            "articleId": article_id,
            "publishedAt": article_published_at(article),
            "receivedAt": received_at,
            "symbols": normalize_article_symbols(article),
            "raw": article,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return {"key": key, "articleId": article_id, "stored": True}


def write_news_symbol_index_to_s3(
    s3,
    bucket,
    prefix,
    article,
    *,
    symbol,
    canonical_key,
    force=False,
    received_at=None,
):
    article_id = article_identifier(article)
    normalized_symbol = normalize_symbol(symbol)
    key = news_symbol_index_key(prefix, normalized_symbol, article_published_at(article), article_id)
    if not force and s3_object_exists(s3, bucket, key):
        return {"key": key, "articleId": article_id, "symbol": normalized_symbol, "stored": False}

    row = news_symbol_index_row(
        article,
        symbol=normalized_symbol,
        canonical_key=canonical_key,
        received_at=received_at,
    )
    body = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/x-ndjson")
    return {"key": key, "articleId": article_id, "symbol": normalized_symbol, "stored": True}


def news_symbol_index_row(article, *, symbol, canonical_key, received_at=None):
    normalized_symbol = normalize_symbol(symbol)
    relevance = classify_subject_relevance(
        target_symbol=normalized_symbol,
        headline=article.get("headline") or article.get("title"),
        summary=article.get("summary"),
        content=article.get("content"),
        symbols=normalize_article_symbols(article),
    )
    return {
        "schemaVersion": 1,
        "source": "alpaca",
        "articleId": article_identifier(article),
        "publishedAt": article_published_at(article),
        "receivedAt": received_at,
        "symbol": normalized_symbol,
        "symbols": normalize_article_symbols(article),
        "targetSymbol": relevance["targetSymbol"],
        "subjectRelevance": relevance["subjectRelevance"],
        "relevanceScoreV2": relevance["relevanceScoreV2"],
        "relevanceReason": relevance["relevanceReason"],
        "directSignals": relevance["directSignals"],
        "canonicalObject": canonical_key,
    }


def write_news_backfill_chunk_marker(s3, bucket, prefix, symbol, start, end, stats, *, force=False):
    key = news_backfill_chunk_marker_key(prefix, symbol, start, end)
    if not force and s3_object_exists(s3, bucket, key):
        return {"key": key, "stored": False}
    body = json.dumps(
        {
            "schemaVersion": 1,
            "dataset": "news",
            "source": "alpaca",
            "symbol": normalize_symbol(symbol),
            "range": {"start": start, "end": end},
            "stats": stats,
            "completedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return {"key": key, "stored": True}


def canonical_news_article_key(prefix, article_id):
    return f"{prefix.strip('/')}/news/articles/article_id={safe_key_part(article_id)}.json"


def news_symbol_index_key(prefix, symbol, published_at, article_id):
    published = parse_article_time(published_at)
    return (
        f"{prefix.strip('/')}/news/index/symbol={safe_key_part(normalize_symbol(symbol))}"
        f"/year={published:%Y}/month={published:%m}/day={published:%d}"
        f"/article_id={safe_key_part(article_id)}.jsonl"
    )


def news_backfill_chunk_marker_key(prefix, symbol, start, end):
    suffix = safe_key_part(f"{normalize_symbol(symbol)}_{start}_{end}")[:160]
    return f"{prefix.strip('/')}/news/manifest/backfill-chunks/{suffix}.json"


def article_published_at(article):
    return (
        article.get("created_at")
        or article.get("createdAt")
        or article.get("published_at")
        or article.get("publishedAt")
        or article.get("updated_at")
        or article.get("updatedAt")
    )


def parse_article_time(value):
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def safe_key_part(value):
    safe = re.sub(r"[^A-Za-z0-9_.=-]+", "_", str(value or "").strip()).strip("._-")
    return safe or "unknown"


def s3_object_exists(s3, bucket, key):
    objects = getattr(s3, "objects", None)
    if isinstance(objects, dict):
        return key in objects
    if isinstance(objects, list):
        return any(item.get("Key") == key for item in objects if isinstance(item, dict))
    if hasattr(s3, "head_object"):
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
    return False
