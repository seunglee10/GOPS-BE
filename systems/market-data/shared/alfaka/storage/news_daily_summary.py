import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from alfaka.storage.clickhouse_loader import clickhouse_time


SUMMARY_VERSION = "v2"
SOURCE_LINK_LIMIT = 3


def build_daily_summary_record(
    *,
    symbol,
    date,
    rows,
    locale="ko-KR",
    model="deterministic",
    generated_at=None,
    status="rolling",
    mention_count=0,
    summary=None,
    key_points=None,
    positive_points=None,
    concerns=None,
    impact_direction=None,
    sentiment=None,
):
    normalized_symbol = str(symbol or "UNKNOWN").strip().upper()
    normalized_rows = [row for row in rows or [] if isinstance(row, dict)]
    article_ids = canonical_article_ids(normalized_rows)
    generated_at = generated_at or utc_now_iso()
    key_points = clean_text_list(key_points if key_points is not None else derive_key_points(normalized_rows), 6, 240)
    positive_points = clean_text_list(positive_points if positive_points is not None else derive_points(normalized_rows, "positive"), 5, 220)
    concerns = clean_text_list(concerns if concerns is not None else derive_points(normalized_rows, "negative"), 5, 220)
    impact_direction = normalize_label(impact_direction or dominant_row_label(normalized_rows, "impactDirection", "impact_direction"), "neutral")
    sentiment = normalize_label(sentiment or dominant_row_label(normalized_rows, "sentiment", "sentiment"), "neutral")
    summary = clean_text(summary or deterministic_summary(normalized_symbol, date, key_points, impact_direction), 700)
    sources = build_daily_summary_sources(normalized_rows, limit=SOURCE_LINK_LIMIT)
    return {
        "date": str(date),
        "symbol": normalized_symbol,
        "locale": locale or "ko-KR",
        "summary": summary,
        "keyPoints": key_points,
        "positivePoints": positive_points,
        "concerns": concerns,
        "impactDirection": impact_direction,
        "sentiment": sentiment,
        "articleIds": article_ids,
        "articleIdsHash": article_ids_hash(article_ids),
        "articleCount": len(article_ids),
        "mentionCount": int(mention_count or 0),
        "status": status or "rolling",
        "model": model or "deterministic",
        "generatedAt": generated_at,
        "version": SUMMARY_VERSION,
        "sources": sources,
    }


def daily_summary_to_clickhouse_row(record):
    return {
        "date": str(record.get("date")),
        "symbol": str(record.get("symbol") or "UNKNOWN").upper(),
        "locale": record.get("locale") or "ko-KR",
        "summary": record.get("summary") or "",
        "key_points": clean_text_list(record.get("keyPoints") or record.get("key_points"), 6, 240),
        "positive_points": clean_text_list(record.get("positivePoints") or record.get("positive_points"), 5, 220),
        "concerns": clean_text_list(record.get("concerns"), 5, 220),
        "impact_direction": normalize_label(record.get("impactDirection") or record.get("impact_direction"), "neutral"),
        "sentiment": normalize_label(record.get("sentiment"), "neutral"),
        "article_ids": [str(item) for item in record.get("articleIds") or record.get("article_ids") or [] if str(item).strip()],
        "article_ids_hash": record.get("articleIdsHash") or record.get("article_ids_hash") or article_ids_hash(record.get("articleIds") or record.get("article_ids") or []),
        "article_count": int(record.get("articleCount") or record.get("article_count") or 0),
        "mention_count": int(record.get("mentionCount") or record.get("mention_count") or 0),
        "status": record.get("status") or "rolling",
        "model": record.get("model") or "deterministic",
        "generated_at": clickhouse_time(record.get("generatedAt") or record.get("generated_at")),
        "version": record.get("version") or SUMMARY_VERSION,
        "raw": json.dumps(record, ensure_ascii=False, separators=(",", ":")),
    }


def clickhouse_row_to_daily_summary(row):
    raw = parse_raw_json(row.get("raw"))
    return {
        "date": str(row.get("date") or ""),
        "symbol": str(row.get("symbol") or "").upper(),
        "locale": row.get("locale") or "ko-KR",
        "summary": row.get("summary") or "",
        "keyPoints": list(row.get("keyPoints") or row.get("key_points") or []),
        "positivePoints": list(row.get("positivePoints") or row.get("positive_points") or []),
        "concerns": list(row.get("concerns") or []),
        "impactDirection": row.get("impactDirection") or row.get("impact_direction") or "neutral",
        "sentiment": row.get("sentiment") or "neutral",
        "articleIds": list(row.get("articleIds") or row.get("article_ids") or []),
        "articleIdsHash": row.get("articleIdsHash") or row.get("article_ids_hash") or "",
        "articleCount": int(row.get("articleCount") or row.get("article_count") or 0),
        "mentionCount": int(row.get("mentionCount") or row.get("mention_count") or 0),
        "status": row.get("status") or "rolling",
        "model": row.get("model") or "",
        "generatedAt": row.get("generatedAt") or row.get("generated_at"),
        "version": row.get("version") or SUMMARY_VERSION,
        "sources": normalize_source_links(row.get("sources") or raw.get("sources")),
        "priceChange": normalize_price_change(row.get("priceChange") or row.get("price_change") or raw.get("priceChange")),
    }


def daily_summary_cache_item(record):
    return {
        key: value
        for key, value in clickhouse_row_to_daily_summary(record).items()
        if key not in {"model"} and value not in (None, "", [])
    }


def canonical_article_ids(rows):
    ids = []
    seen = set()
    for row in rows or []:
        article_id = str(row.get("articleId") or row.get("article_id") or "").strip()
        if article_id and article_id not in seen:
            ids.append(article_id)
            seen.add(article_id)
    return sorted(ids)


def build_daily_summary_sources(rows, limit=SOURCE_LINK_LIMIT):
    sources = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = clean_text(row.get("url"), 500)
        title = clean_text(
            row.get("localizedHeadline")
            or row.get("localized_headline")
            or row.get("headline")
            or row.get("title"),
            180,
        )
        if not url or not title:
            continue
        article_id = clean_text(row.get("articleId") or row.get("article_id"), 120)
        key = article_id or url
        if key in seen:
            continue
        seen.add(key)
        source = {
            "title": title,
            "url": url,
        }
        if article_id:
            source["articleId"] = article_id
        name = clean_text(row.get("source"), 80)
        if name:
            source["name"] = name
        published_at = clean_text(row.get("publishedAt") or row.get("published_at"), 40)
        if published_at:
            source["publishedAt"] = published_at
        sources.append(source)
        if len(sources) >= int(limit):
            break
    return sources


def normalize_source_links(value):
    if not isinstance(value, list):
        return []
    sources = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("url"), 500)
        title = clean_text(item.get("title"), 180)
        if not url or not title:
            continue
        key = clean_text(item.get("articleId") or item.get("article_id"), 120) or url
        if key in seen:
            continue
        seen.add(key)
        source = {"title": title, "url": url}
        article_id = clean_text(item.get("articleId") or item.get("article_id"), 120)
        if article_id:
            source["articleId"] = article_id
        name = clean_text(item.get("name") or item.get("source"), 80)
        if name:
            source["name"] = name
        published_at = clean_text(item.get("publishedAt") or item.get("published_at"), 40)
        if published_at:
            source["publishedAt"] = published_at
        sources.append(source)
        if len(sources) >= SOURCE_LINK_LIMIT:
            break
    return sources


def attach_price_changes_to_daily_summaries(summaries, candles):
    price_changes = daily_price_changes(candles)
    if not price_changes:
        return list(summaries or [])
    enriched = []
    for item in summaries or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        date = summary_date_key(next_item.get("date"))
        price_change = price_changes.get(date)
        if price_change:
            next_item["priceChange"] = price_change
        enriched.append(next_item)
    return enriched


def daily_price_changes(candles):
    normalized = []
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        date = summary_date_key(candle.get("timestamp") or candle.get("date"))
        close = float_or_none(candle.get("close"))
        if not date or close is None:
            continue
        normalized.append((date, close))
    normalized.sort(key=lambda item: item[0])
    changes = {}
    previous_close = None
    for date, close in normalized:
        if previous_close is not None:
            change = close - previous_close
            changes[date] = {
                "date": date,
                "previousClose": round(previous_close, 6),
                "close": round(close, 6),
                "change": round(change, 6),
                "changePercent": round((change / previous_close) * 100, 6) if previous_close else 0,
            }
        previous_close = close
    return changes


def normalize_price_change(value):
    if not isinstance(value, dict):
        return None
    date = summary_date_key(value.get("date"))
    previous_close = float_or_none(value.get("previousClose") if "previousClose" in value else value.get("previous_close"))
    close = float_or_none(value.get("close"))
    change = float_or_none(value.get("change"))
    if not date or previous_close is None or close is None or change is None:
        return None
    change_percent = float_or_none(value.get("changePercent") if "changePercent" in value else value.get("change_percent"))
    return {
        "date": date,
        "previousClose": round(previous_close, 6),
        "close": round(close, 6),
        "change": round(change, 6),
        "changePercent": round(change_percent if change_percent is not None else ((change / previous_close) * 100 if previous_close else 0), 6),
    }


def summary_date_key(value):
    if not value:
        return ""
    text = str(value).strip()
    return text[:10] if len(text) >= 10 else text


def float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def article_ids_hash(article_ids):
    encoded = "\n".join(sorted(str(item) for item in article_ids or [] if str(item).strip()))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def parse_raw_json(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def derive_key_points(rows):
    points = []
    for row in rows or []:
        row_points = row.get("keyPoints") or row.get("key_points") or []
        if isinstance(row_points, list) and row_points:
            points.extend(row_points)
            continue
        headline = row.get("localizedHeadline") or row.get("localized_headline") or row.get("headline")
        summary = row.get("localizedSummary") or row.get("localized_summary") or row.get("summary")
        if headline and summary:
            points.append(f"{headline}: {summary}")
        elif headline:
            points.append(headline)
        elif summary:
            points.append(summary)
    return points


def derive_points(rows, direction):
    key = "positivePoints" if direction == "positive" else "concerns"
    snake_key = "positive_points" if direction == "positive" else "concerns"
    points = []
    for row in rows or []:
        if normalize_label(row.get("impactDirection") or row.get("impact_direction"), "neutral") != direction:
            continue
        values = row.get(key) or row.get(snake_key) or []
        if isinstance(values, list) and values:
            points.extend(values)
        else:
            summary = row.get("localizedSummary") or row.get("localized_summary") or row.get("summary")
            if summary:
                points.append(summary)
    return points


def dominant_row_label(rows, camel_key, snake_key):
    counter = Counter()
    for row in rows or []:
        label = normalize_label(row.get(camel_key) or row.get(snake_key), "neutral")
        counter[label] += 1
    if not counter:
        return "neutral"
    return counter.most_common(1)[0][0]


def deterministic_summary(symbol, date, key_points, impact_direction):
    count = len(key_points or [])
    if count:
        return f"{date} {symbol} 관련 주요 뉴스 {count}건을 요약했습니다. 영향 방향은 {impact_direction_label(impact_direction)}로 분류했습니다."
    return f"{date} {symbol} 관련 직접 뉴스가 충분하지 않습니다."


def impact_direction_label(value):
    return {
        "positive": "긍정",
        "negative": "부정",
        "mixed": "혼재",
        "neutral": "중립",
    }.get(str(value or "").lower(), "중립")


def normalize_label(value, fallback):
    normalized = str(value or fallback).strip().lower()
    return normalized if normalized in {"positive", "negative", "neutral", "mixed"} else fallback


def clean_text(value, max_length):
    return " ".join(str(value or "").split())[: int(max_length)]


def clean_text_list(values, max_items, max_length):
    if isinstance(values, str):
        source = [values]
    elif isinstance(values, list):
        source = values
    else:
        source = []
    result = []
    seen = set()
    for value in source:
        text = clean_text(value, max_length)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
        if len(result) >= int(max_items):
            break
    return result


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
