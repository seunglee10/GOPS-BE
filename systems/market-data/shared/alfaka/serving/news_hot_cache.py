import json
import os
from datetime import datetime, timezone

from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.news.relevance import classify_subject_relevance, is_direct_subject, normalize_subject_level
from alfaka.storage.news_daily_summary import daily_summary_cache_item

DEFAULT_NEWS_TTL_SECONDS = 2592000
DEFAULT_NEWS_MAX_ITEMS = 1000
DEFAULT_NEWS_RETENTION_DAYS = 30
DEFAULT_DAILY_TTL_SECONDS = 2592000
DEFAULT_DAILY_COVERAGE_TTL_SECONDS = 2592000


def write_localized_news_to_redis(
    redis_client,
    row,
    *,
    ttl_seconds=DEFAULT_NEWS_TTL_SECONDS,
    max_items=DEFAULT_NEWS_MAX_ITEMS,
    retention_days=DEFAULT_NEWS_RETENTION_DAYS,
    locale="ko-KR",
    topics=None,
):
    if redis_client is None or not row:
        return
    keys = RedisKeyBuilder()
    item = localized_news_cache_item(row)
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    score = timestamp_score(item.get("publishedAt") or row.get("published_at"))
    target_symbol = str(item.get("targetSymbol") or item.get("symbol") or "").strip().upper()
    if target_symbol:
        write_news_cache_member(
            redis_client,
            keys.news_latest_v2(locale, target_symbol),
            encoded,
            score,
            ttl_seconds,
            max_items,
            retention_days=retention_days,
        )
    for topic in topics or []:
        topic_value = str(topic or "").strip()
        if topic_value:
            write_news_cache_member(
                redis_client,
                keys.news_topic_v2(locale, topic_value),
                encoded,
                score,
                ttl_seconds,
                max_items,
                retention_days=retention_days,
            )


def read_localized_news_from_redis(redis_client, symbol, *, limit=10, locale="ko-KR"):
    if redis_client is None:
        return []
    keys = RedisKeyBuilder()
    key = keys.news_latest_v2(locale, str(symbol or "").strip().upper())
    members = redis_zrevrange(redis_client, key, 0, max(0, int(limit) - 1))
    rows = []
    for member in members:
        try:
            decoded = json.loads(member)
        except Exception:
            continue
        if isinstance(decoded, dict):
            rows.append(decoded)
    return rows


def read_localized_topic_news_from_redis(redis_client, topic, *, limit=10, locale="ko-KR"):
    if redis_client is None:
        return []
    keys = RedisKeyBuilder()
    key = keys.news_topic_v2(locale, str(topic or "").strip())
    members = redis_zrevrange(redis_client, key, 0, max(0, int(limit) - 1))
    rows = []
    for member in members:
        try:
            decoded = json.loads(member)
        except Exception:
            continue
        if isinstance(decoded, dict):
            rows.append(decoded)
    return rows


def write_company_daily_summary_to_redis(redis_client, record, *, ttl_seconds=DEFAULT_DAILY_TTL_SECONDS, max_items=30, locale="ko-KR"):
    if redis_client is None or not record:
        return
    item = daily_summary_cache_item(record)
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol:
        return
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    score = daily_summary_score(item)
    keys = RedisKeyBuilder()
    write_news_cache_member(redis_client, keys.news_daily_v2(locale, symbol), encoded, score, ttl_seconds, max_items)


def write_company_daily_summaries_to_redis(
    redis_client,
    rows,
    *,
    symbol,
    days=30,
    limit=30,
    ttl_seconds=None,
    coverage_ttl_seconds=None,
    locale="ko-KR",
):
    if redis_client is None:
        return []
    ttl = int(ttl_seconds if ttl_seconds is not None else os.getenv("NEWS_DAILY_REDIS_TTL_SECONDS", str(DEFAULT_DAILY_TTL_SECONDS)))
    max_items = max(1, int(limit))
    normalized_rows = dedupe_daily_summary_rows([row for row in rows or [] if isinstance(row, dict)])
    for row in normalized_rows[:max_items]:
        write_company_daily_summary_to_redis(redis_client, row, ttl_seconds=ttl, max_items=max_items, locale=locale)
    write_company_daily_summary_coverage_to_redis(
        redis_client,
        symbol=symbol,
        rows=normalized_rows,
        days=days,
        limit=limit,
        ttl_seconds=coverage_ttl_seconds,
        locale=locale,
    )
    return normalized_rows[:max_items]


def read_company_daily_summaries_from_redis(redis_client, symbol, *, limit=5, locale="ko-KR"):
    if redis_client is None:
        return []
    keys = RedisKeyBuilder()
    key = keys.news_daily_v2(locale, str(symbol or "").strip().upper())
    members = redis_zrevrange(redis_client, key, 0, -1)
    rows = []
    for member in members:
        try:
            decoded = json.loads(member)
        except Exception:
            continue
        if isinstance(decoded, dict):
            rows.append(decoded)
    return dedupe_daily_summary_rows(rows)[: max(0, int(limit))]


def write_company_daily_summary_coverage_to_redis(
    redis_client,
    *,
    symbol,
    rows,
    days=30,
    limit=30,
    ttl_seconds=None,
    locale="ko-KR",
):
    if redis_client is None:
        return None
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None
    normalized_rows = dedupe_daily_summary_rows([row for row in rows or [] if isinstance(row, dict)])
    dates = [str(row.get("date") or "").strip() for row in normalized_rows if str(row.get("date") or "").strip()]
    coverage = {
        "symbol": normalized_symbol,
        "locale": str(locale or "ko-KR"),
        "days": int(days),
        "limit": int(limit),
        "rowCount": len(normalized_rows),
        "oldestDate": min(dates) if dates else None,
        "newestDate": max(dates) if dates else None,
        "refreshedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "coverageType": "complete",
    }
    ttl = int(ttl_seconds if ttl_seconds is not None else os.getenv("NEWS_DAILY_REDIS_COVERAGE_TTL_SECONDS", str(DEFAULT_DAILY_COVERAGE_TTL_SECONDS)))
    key = RedisKeyBuilder().news_daily_coverage_v2(locale, normalized_symbol)
    encoded = json.dumps(coverage, ensure_ascii=False, separators=(",", ":"))
    if hasattr(redis_client, "setex") and ttl > 0:
        redis_client.setex(key, ttl, encoded)
    else:
        try:
            redis_client.set(key, encoded, ex=ttl if ttl > 0 else None)
        except TypeError:
            redis_client.set(key, encoded)
            if ttl > 0 and hasattr(redis_client, "expire"):
                redis_client.expire(key, ttl)
    return coverage


def read_company_daily_summary_coverage_from_redis(redis_client, symbol, *, locale="ko-KR"):
    if redis_client is None:
        return None
    key = RedisKeyBuilder().news_daily_coverage_v2(locale, str(symbol or "").strip().upper())
    value = redis_client.get(key)
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def company_daily_summary_coverage_valid(coverage, *, symbol, days=30, limit=30, locale="ko-KR", rows=None):
    if not isinstance(coverage, dict):
        return False
    if str(coverage.get("coverageType") or "") != "complete":
        return False
    if str(coverage.get("symbol") or "").strip().upper() != str(symbol or "").strip().upper():
        return False
    if str(coverage.get("locale") or "ko-KR").lower() != str(locale or "ko-KR").lower():
        return False
    if int_or_default(coverage.get("days"), 0) < int(days):
        return False
    if int_or_default(coverage.get("limit"), 0) < int(limit):
        return False
    row_count = int_or_default(coverage.get("rowCount"), 0)
    if rows is not None and row_count > 0:
        return len(rows) >= min(row_count, int(limit))
    return True


def dedupe_daily_summary_rows(rows):
    best_by_date = {}
    without_date = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        date_value = str(row.get("date") or "").strip()
        if not date_value:
            without_date.append(row)
            continue
        existing = best_by_date.get(date_value)
        if existing is None or daily_summary_generated_at_score(row) >= daily_summary_generated_at_score(existing):
            best_by_date[date_value] = row
    deduped = list(best_by_date.values()) + without_date
    return sorted(deduped, key=daily_summary_sort_key, reverse=True)


def daily_summary_sort_key(row):
    return (
        str(row.get("date") or ""),
        daily_summary_generated_at_score(row),
    )


def daily_summary_generated_at_score(row):
    value = row.get("generatedAt") or row.get("generated_at")
    return timestamp_score(value) if value else 0.0


def int_or_default(value, fallback):
    try:
        return int(value)
    except Exception:
        return int(fallback)


def write_news_cache_member(redis_client, key, encoded, score, ttl_seconds, max_items, *, retention_days=None):
    redis_client.zadd(key, {encoded: score})
    if retention_days is not None and int(retention_days) > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - int(retention_days) * 86400
        redis_zremrangebyscore(redis_client, key, float("-inf"), cutoff)
    limit = max(1, int(max_items))
    members = redis_client.zrange(key, 0, -1)
    if len(members) > limit:
        redis_client.zremrangebyrank(key, 0, len(members) - limit - 1)
    if ttl_seconds > 0:
        redis_client.expire(key, int(ttl_seconds))


def redis_zrevrange(redis_client, key, start, end):
    if hasattr(redis_client, "zrevrange"):
        return redis_client.zrevrange(key, start, end)
    values = redis_client.zrange(key, 0, -1)
    ordered = list(reversed(values))
    length = len(ordered)
    if start < 0:
        start = length + start
    if end < 0:
        end = length + end
    start = max(0, start)
    end = min(length - 1, end)
    if start > end or length == 0:
        return []
    return ordered[start : end + 1]


def redis_zremrangebyscore(redis_client, key, start, end):
    if hasattr(redis_client, "zremrangebyscore"):
        return redis_client.zremrangebyscore(key, start, end)
    values = getattr(redis_client, "zsets", {}).get(key, {})
    removed = [member for member, score in list(values.items()) if start <= score <= end]
    for member in removed:
        values.pop(member, None)
    return len(removed)


def localized_news_cache_item(row):
    raw = parse_raw(row.get("raw"))
    symbols = normalized_symbols(row.get("symbols") or raw.get("symbols") or [row.get("symbol")])
    target_symbol = str(row.get("targetSymbol") or row.get("target_symbol") or row.get("symbol") or (symbols[0] if symbols else "UNKNOWN")).upper()
    localized_headline = row.get("localizedHeadline") or row.get("localized_headline") or row.get("headline") or raw.get("headline")
    localized_summary = row.get("localizedSummary") or row.get("localized_summary") or row.get("summary") or raw.get("summary")
    headline = row.get("headline") or raw.get("headline") or localized_headline
    summary = row.get("summary") or raw.get("summary") or localized_summary
    relevance = classify_subject_relevance(
        target_symbol=target_symbol,
        headline=headline,
        summary=summary,
        content=raw.get("content"),
        symbols=symbols,
    )
    subject_relevance = normalize_subject_level(row.get("subjectRelevance") or row.get("subject_relevance") or relevance["subjectRelevance"])
    return {
        "symbol": str(row.get("symbol") or target_symbol or (symbols[0] if symbols else "UNKNOWN")).upper(),
        "targetSymbol": target_symbol,
        "symbols": symbols,
        "subjectRelevance": subject_relevance,
        "relevanceScoreV2": float_or_default(row.get("relevanceScoreV2") or row.get("relevance_score_v2"), relevance["relevanceScoreV2"]),
        "relevanceReason": row.get("relevanceReason") or row.get("relevance_reason") or relevance["relevanceReason"],
        "directSignals": row.get("directSignals") or row.get("direct_signals") or relevance["directSignals"],
        "isDirectSubject": is_direct_subject(subject_relevance),
        "articleId": str(row.get("articleId") or row.get("article_id") or raw.get("articleId") or ""),
        "headline": headline,
        "summary": summary,
        "localizedHeadline": localized_headline,
        "localizedSummary": localized_summary,
        "keyPoints": row.get("keyPoints") or row.get("key_points") or [],
        "positivePoints": row.get("positivePoints") or row.get("positive_points") or [],
        "concerns": row.get("concerns") or [],
        "url": row.get("url") or raw.get("url"),
        "source": row.get("source") or raw.get("source"),
        "publishedAt": row.get("publishedAt") or row.get("published_at") or raw.get("publishedAt"),
        "receivedAt": row.get("receivedAt") or row.get("received_at") or raw.get("receivedAt"),
        "eventType": row.get("eventType") or row.get("event_type"),
        "sentiment": row.get("sentiment"),
        "impactDirection": row.get("impactDirection") or row.get("impact_direction"),
        "whyItMatters": row.get("whyItMatters") or row.get("why_it_matters"),
        "model": row.get("model"),
        "localizedAt": row.get("localizedAt") or row.get("localized_at"),
    }


def normalized_symbols(values):
    result = []
    for value in values or []:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def parse_raw(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def float_or_default(value, fallback):
    try:
        return float(value)
    except Exception:
        return float(fallback or 0.0)


def timestamp_score(value):
    if not value:
        return datetime.now(timezone.utc).timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:
        return datetime.now(timezone.utc).timestamp()


def daily_summary_score(item):
    date_value = str(item.get("date") or "")
    try:
        return datetime.fromisoformat(f"{date_value}T00:00:00+00:00").timestamp()
    except Exception:
        return timestamp_score(item.get("generatedAt"))
