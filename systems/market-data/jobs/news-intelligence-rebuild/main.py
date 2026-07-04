# 역할: 기존 뉴스 localization row에 relevance v2 필드를 다시 계산해 적재합니다.
# 사용: 배포 후 최근 30일 뉴스 cache/schema 전환을 한 번 보정하는 Kubernetes Job으로 실행합니다.
import os

from alfaka.common.env import load_dotenv
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient
from alfaka.storage.news_intelligence import build_news_intelligence_record, news_intelligence_to_clickhouse_row, utc_now_iso


def main():
    load_dotenv()
    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    client.ensure_market_data_schema()
    days = int(os.getenv("NEWS_INTELLIGENCE_REBUILD_DAYS", "30"))
    batch_size = int(os.getenv("NEWS_INTELLIGENCE_REBUILD_BATCH_SIZE", "500"))
    max_rows = int(os.getenv("NEWS_INTELLIGENCE_REBUILD_MAX_ROWS", "5000"))
    dry_run = bool_env(os.getenv("NEWS_INTELLIGENCE_REBUILD_DRY_RUN"), default=False)
    if dry_run:
        total = count_recent_localizations(client, days=days, max_rows=max_rows)
        print(f"News intelligence rebuild dry-run: rows={total} days={days} maxRows={max_rows}", flush=True)
        return

    total = rebuild_recent_localizations(client, days=days, batch_size=batch_size, max_rows=max_rows)
    print(f"News intelligence rebuild 완료: rows={total} days={days}", flush=True)


def rebuild_recent_localizations(client, *, days=30, batch_size=500, max_rows=5000):
    rows = []
    offset = 0
    while len(rows) < max_rows:
        batch = read_localization_batch(client, days=days, limit=min(batch_size, max_rows - len(rows)), offset=offset)
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)

    total = 0
    for batch in chunked(dedupe_localization_rows(rows), batch_size):
        rebuilt = [news_intelligence_to_clickhouse_row(rebuild_localization_record(row)) for row in batch]
        delete_localization_rows(client, rebuilt)
        client.insert_json_each_row("news_article_localizations", rebuilt)
        total += len(rebuilt)
    return total


def read_localization_batch(client, *, days, limit, offset):
    query = f"""
    SELECT
      formatDateTime(published_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS publishedAt,
      symbol,
      article_id AS articleId,
      locale,
      symbols,
      headline,
      summary,
      url,
      source,
      localized_headline AS localizedHeadline,
      localized_summary AS localizedSummary,
      key_points AS keyPoints,
      positive_points AS positivePoints,
      concerns,
      event_type AS eventType,
      sentiment,
      impact_direction AS impactDirection,
      why_it_matters AS whyItMatters,
      model,
      raw
    FROM {client.database}.news_article_localizations
    WHERE published_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
    ORDER BY published_at DESC, localized_at DESC
    LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
    FORMAT JSONEachRow
    """
    return client.query_json_each_row(query, {"days": int(days), "limit": int(limit), "offset": int(offset)})


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
