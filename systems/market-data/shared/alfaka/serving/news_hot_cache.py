import json
from datetime import datetime, timezone

from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.news.relevance import classify_subject_relevance, is_direct_subject, normalize_subject_level
from alfaka.storage.news_daily_summary import daily_summary_cache_item


def write_localized_news_to_redis(redis_client, row, *, ttl_seconds=1800, max_items=20, locale="ko-KR", topics=None):
    if redis_client is None or not row:
        return
    keys = RedisKeyBuilder()
    item = localized_news_cache_item(row)
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    score = timestamp_score(item.get("publishedAt") or row.get("published_at"))
    target_symbol = str(item.get("targetSymbol") or item.get("symbol") or "").strip().upper()
    if target_symbol:
        write_news_cache_member(redis_client, keys.news_latest_v2(locale, target_symbol), encoded, score, ttl_seconds, max_items)
    for topic in topics or []:
        topic_value = str(topic or "").strip()
        if topic_value:
            write_news_cache_member(redis_client, keys.news_topic_v2(locale, topic_value), encoded, score, ttl_seconds, max_items)


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


def write_company_daily_summary_to_redis(redis_client, record, *, ttl_seconds=86400, max_items=30, locale="ko-KR"):
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


def read_company_daily_summaries_from_redis(redis_client, symbol, *, limit=5, locale="ko-KR"):
    if redis_client is None:
        return []
    keys = RedisKeyBuilder()
    key = keys.news_daily_v2(locale, str(symbol or "").strip().upper())
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


def write_news_cache_member(redis_client, key, encoded, score, ttl_seconds, max_items):
    redis_client.zadd(key, {encoded: score})
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
    return list(reversed(values))[start : end + 1]


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
