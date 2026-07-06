# 역할: 기존 뉴스 localization row에 relevance v2 필드를 다시 계산해 적재합니다.
# 사용: 배포 후 최근 30일 뉴스 cache/schema 전환을 한 번 보정하는 Kubernetes Job으로 실행합니다.
import os

import redis

from alfaka.common.env import load_dotenv
from alfaka.serving.news_hot_cache import (
    DEFAULT_NEWS_MAX_ITEMS,
    DEFAULT_NEWS_RETENTION_DAYS,
    DEFAULT_NEWS_TTL_SECONDS,
    write_localized_news_to_redis,
)
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, should_ensure_schema_on_start
from alfaka.storage.news_intelligence import build_news_intelligence_record, news_intelligence_to_clickhouse_row, utc_now_iso


def main():
    load_dotenv()
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    if should_ensure_schema_on_start():
        client.ensure_market_data_schema()
    days = int(os.getenv("NEWS_INTELLIGENCE_REBUILD_DAYS", "30"))
    batch_size = int(os.getenv("NEWS_INTELLIGENCE_REBUILD_BATCH_SIZE", "500"))
    max_rows = int(os.getenv("NEWS_INTELLIGENCE_REBUILD_MAX_ROWS", "5000"))
    dry_run = bool_env(os.getenv("NEWS_INTELLIGENCE_REBUILD_DRY_RUN"), default=False)
    if dry_run:
        total = count_recent_localizations(client, days=days, max_rows=max_rows)
        print(f"News intelligence rebuild dry-run: rows={total} days={days} maxRows={max_rows}", flush=True)
        return

    warm_redis = bool_env(os.getenv("NEWS_INTELLIGENCE_REBUILD_WARM_REDIS"), default=True)
    rewrite_clickhouse = bool_env(os.getenv("NEWS_INTELLIGENCE_REBUILD_REWRITE_CLICKHOUSE"), default=False)
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True) if warm_redis else None
    total = rebuild_recent_localizations(
        client,
        days=days,
        batch_size=batch_size,
        max_rows=max_rows,
        redis_client=redis_client,
        rewrite_clickhouse=rewrite_clickhouse,
    )
    print(
        f"News intelligence rebuild 완료: rows={total} days={days} "
        f"redisWarm={warm_redis} clickhouseRewrite={rewrite_clickhouse}",
        flush=True,
    )


def rebuild_recent_localizations(client, *, days=30, batch_size=500, max_rows=5000, redis_client=None, rewrite_clickhouse=False):
    columns = read_localization_columns(client)
    rows = read_recent_localization_rows(client, days=days, batch_size=batch_size, max_rows=max_rows, columns=columns)

    total = 0
    for batch in chunked(dedupe_localization_rows(rows), batch_size):
        if rewrite_clickhouse:
            records = [rebuild_localization_record(row) for row in batch]
            rebuilt = [news_intelligence_to_clickhouse_row(record) for record in records]
            delete_localization_rows(client, rebuilt)
            client.insert_json_each_row("news_article_localizations", rebuilt)
            warm_localization_records_to_redis(redis_client, records)
            total += len(rebuilt)
            continue
        warm_localization_records_to_redis(redis_client, batch)
        total += len(batch)
    return total


def warm_localization_records_to_redis(redis_client, records):
    if redis_client is None:
        return 0
    ttl_seconds = int(os.getenv("NEWS_REDIS_TTL_SECONDS", str(DEFAULT_NEWS_TTL_SECONDS)))
    max_items = int(os.getenv("NEWS_REDIS_MAX_ITEMS", str(DEFAULT_NEWS_MAX_ITEMS)))
    retention_days = int(os.getenv("NEWS_REDIS_RETENTION_DAYS", str(DEFAULT_NEWS_RETENTION_DAYS)))
    warmed = 0
    for record in records or []:
        if not isinstance(record, dict):
            continue
        write_localized_news_to_redis(
            redis_client,
            record,
            ttl_seconds=ttl_seconds,
            max_items=max_items,
            retention_days=retention_days,
            locale=record.get("locale") or os.getenv("NEWS_INTELLIGENCE_LOCALE", "ko-KR"),
        )
        warmed += 1
    return warmed


def read_recent_localization_rows(client, *, days, batch_size, max_rows, columns=None):
    rows = []
    partitions = read_localization_partitions(client, days=days, limit=max(1, int(max_rows or 1)))
    if not partitions:
        return rows
    limit_per_symbol = max(1, min(int(batch_size or 1), (int(max_rows or 1) + len(partitions) - 1) // len(partitions)))
    for partition in partitions:
        if len(rows) >= max_rows:
            break
        symbol = str(partition.get("symbol") or "").strip().upper()
        locale = str(partition.get("locale") or "ko-KR").strip() or "ko-KR"
        if not symbol:
            continue
        batch = read_localization_batch(
            client,
            days=days,
            limit=min(limit_per_symbol, max_rows - len(rows)),
            columns=columns,
            symbol=symbol,
            locale=locale,
        )
        rows.extend(batch)
    return rows[:max_rows]


def read_localization_columns(client):
    query = f"""
    SELECT name
    FROM system.columns
    WHERE database = {{database:String}}
      AND table = 'news_article_localizations'
    FORMAT JSONEachRow
    """
    try:
        rows = client.query_json_each_row(query, {"database": client.database})
    except Exception as exc:
        print(f"News intelligence rebuild schema discovery failed: {type(exc).__name__}: {exc}", flush=True)
        return set()
    return {str(row.get("name") or "").strip() for row in rows if str(row.get("name") or "").strip()}


def read_localization_partitions(client, *, days, limit):
    query = f"""
    SELECT symbol, locale
    FROM {client.database}.news_article_localizations
    WHERE published_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
    GROUP BY symbol, locale
    LIMIT {{limit:UInt32}}
    FORMAT JSONEachRow
    """
    return client.query_json_each_row(query, {"days": int(days), "limit": int(limit)})


def read_localization_batch(client, *, days, limit, columns=None, symbol=None, locale=None):
    where_clauses = ["published_at >= now64(3) - INTERVAL {days:UInt32} DAY"]
    parameters = {"days": int(days), "limit": int(limit)}
    if symbol:
        where_clauses.append("symbol = {symbol:String}")
        parameters["symbol"] = str(symbol).strip().upper()
    if locale:
        where_clauses.append("locale = {locale:String}")
        parameters["locale"] = str(locale).strip() or "ko-KR"
    where_sql = "\n      AND ".join(where_clauses)
    order_sql = "ORDER BY published_at DESC, article_id DESC" if symbol and locale else ""
    query = f"""
    SELECT
      formatDateTime(published_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS publishedAt,
      symbol,
      article_id AS articleId,
      locale,
      {localization_select_expr(columns, 'symbols', 'symbols', '[symbol]')},
      {localization_select_expr(columns, 'target_symbol', 'targetSymbol', 'symbol')},
      {localization_select_expr(columns, 'subject_relevance', 'subjectRelevance', "'mention'")},
      {localization_select_expr(columns, 'relevance_score_v2', 'relevanceScoreV2', 'toFloat32(0)')},
      {localization_select_expr(columns, 'relevance_reason', 'relevanceReason', "''")},
      {localization_select_expr(columns, 'direct_signals', 'directSignals', "CAST([], 'Array(String)')")},
      headline,
      summary,
      url,
      source,
      localized_headline AS localizedHeadline,
      localized_summary AS localizedSummary,
      {localization_select_expr(columns, 'key_points', 'keyPoints', "CAST([], 'Array(String)')")},
      {localization_select_expr(columns, 'positive_points', 'positivePoints', "CAST([], 'Array(String)')")},
      {localization_select_expr(columns, 'concerns', 'concerns', "CAST([], 'Array(String)')")},
      event_type AS eventType,
      sentiment,
      impact_direction AS impactDirection,
      why_it_matters AS whyItMatters,
      model,
      formatDateTime(localized_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS localizedAt,
      raw
    FROM {client.database}.news_article_localizations
    WHERE {where_sql}
    {order_sql}
    LIMIT {{limit:UInt32}}
    FORMAT JSONEachRow
    """
    return client.query_json_each_row(query, parameters)


def localization_select_expr(columns, column, alias, fallback):
    if columns and column in columns:
        return f"{column} AS {alias}" if column != alias else column
    return f"{fallback} AS {alias}"


def count_recent_localizations(client, *, days, max_rows):
    query = f"""
    SELECT count()
    FROM
    (
      SELECT article_id, locale
      FROM {client.database}.news_article_localizations
      WHERE published_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
      GROUP BY article_id, locale
      LIMIT {{maxRows:UInt32}}
    )
    FORMAT JSONEachRow
    """
    rows = client.query_json_each_row(query, {"days": int(days), "maxRows": int(max_rows)})
    if not rows:
        return 0
    return int(rows[0].get("count()") or rows[0].get("count") or 0)


def rebuild_localization_record(row):
    event = {
        "eventType": "NEWS_ARTICLE",
        "symbol": row.get("symbol"),
        "articleId": row.get("articleId"),
        "headline": row.get("headline") or row.get("localizedHeadline"),
        "summary": row.get("summary") or row.get("localizedSummary"),
        "publishedAt": row.get("publishedAt"),
        "url": row.get("url"),
        "source": row.get("source"),
        "symbols": row.get("symbols") or [row.get("symbol")],
        "raw": row.get("raw"),
    }
    intelligence = {
        "localizedHeadline": row.get("localizedHeadline") or row.get("headline"),
        "localizedSummary": row.get("localizedSummary") or row.get("summary") or row.get("headline"),
        "keyPoints": row.get("keyPoints") or [],
        "positivePoints": row.get("positivePoints") or [],
        "concerns": row.get("concerns") or [],
        "eventType": row.get("eventType") or "general",
        "sentiment": row.get("sentiment") or "neutral",
        "impactDirection": row.get("impactDirection") or "neutral",
        "whyItMatters": row.get("whyItMatters") or "",
    }
    return build_news_intelligence_record(
        event,
        intelligence,
        locale=row.get("locale") or "ko-KR",
        model=row.get("model") or "relevance-v2-rebuild",
        localized_at=utc_now_iso(),
    )


def dedupe_localization_rows(rows):
    deduped = {}
    for row in rows:
        article_id = str(row.get("articleId") or "").strip()
        locale = str(row.get("locale") or "ko-KR").strip() or "ko-KR"
        if not article_id:
            continue
        deduped.setdefault((article_id, locale), row)
    return list(deduped.values())


def delete_localization_rows(client, rows):
    article_ids = sorted({str(row.get("article_id") or "").strip() for row in rows if row.get("article_id")})
    locales = sorted({str(row.get("locale") or "ko-KR").strip() or "ko-KR" for row in rows})
    if not article_ids or not locales:
        return
    client.execute(
        f"""
        ALTER TABLE {client.database}.news_article_localizations
        DELETE WHERE article_id IN {{articleIds:Array(String)}}
          AND locale IN {{locales:Array(String)}}
        SETTINGS mutations_sync = 1
        """,
        {"articleIds": article_ids, "locales": locales},
    )


def chunked(values, size):
    size = max(1, int(size or 1))
    for index in range(0, len(values), size):
        yield values[index:index + size]


def bool_env(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()
