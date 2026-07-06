# 역할: 기사 단위 intelligence를 회사/날짜 단위 daily brief로 모아 ClickHouse와 Redis에 저장합니다.
# 사용: Kafka topic market.news.daily-summary-dirty.v1을 consume하는 Docker/EKS worker로 실행합니다.
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.kafka_io import create_json_consumer
from alfaka.serving.news_hot_cache import write_company_daily_summary_to_redis
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, should_ensure_schema_on_start
from alfaka.storage.news_daily_summary import (
    article_ids_hash,
    build_daily_summary_record,
    clickhouse_row_to_daily_summary,
    canonical_article_ids,
    daily_summary_to_clickhouse_row,
    utc_now_iso,
)


def main():
    load_dotenv()
    if not bool_env("NEWS_DAILY_SUMMARY_ENABLED", True):
        print("News daily summary worker disabled: NEWS_DAILY_SUMMARY_ENABLED=false", flush=True)
        return

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_NEWS_DAILY_SUMMARY_DIRTY_TOPIC", "market.news.daily-summary-dirty.v1")
    group_id = os.getenv("NEWS_DAILY_SUMMARY_GROUP_ID", "alfaka-news-daily-summary-worker")
    enable_auto_commit = bool_env("NEWS_DAILY_SUMMARY_ENABLE_AUTO_COMMIT", False)
    clickhouse = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    if should_ensure_schema_on_start():
        clickhouse.ensure_market_data_schema()
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    consumer = create_json_consumer(
        [topic],
        kafka_servers,
        group_id,
        "alfaka-news-daily-summary-consumer",
        enable_auto_commit=enable_auto_commit,
        max_poll_interval_ms=int_env("NEWS_DAILY_SUMMARY_MAX_POLL_INTERVAL_MS", 900000),
        max_poll_records=int_env("NEWS_DAILY_SUMMARY_MAX_POLL_RECORDS", 5),
    )
    print(f"News daily summary worker 시작: topic={topic} group={group_id}", flush=True)

    for record in consumer:
        try:
            process_dirty_event(record.value, clickhouse_client=clickhouse, redis_client=redis_client)
            if not enable_auto_commit:
                consumer.commit()
        except Exception as exc:
            print(f"News daily summary 처리 실패: {exc}; payload={json.dumps(record.value, ensure_ascii=False)}", file=sys.stderr, flush=True)


def process_dirty_event(
    event,
    *,
    clickhouse_client,
    redis_client=None,
    summarize_fn=None,
    locale=None,
    model=None,
    ttl_seconds=None,
    max_items=None,
):
    if not isinstance(event, dict) or event.get("eventType") != "NEWS_DAILY_SUMMARY_DIRTY":
        return None
    symbol = str(event.get("symbol") or "").strip().upper()
    date = str(event.get("date") or "").strip()
    if not symbol or not date:
        return None

    locale = locale or event.get("locale") or os.getenv("NEWS_INTELLIGENCE_LOCALE", "ko-KR")
    model = model or os.getenv("NEWS_DAILY_SUMMARY_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    rows = read_daily_candidate_rows(clickhouse_client, symbol=symbol, date=date, locale=locale)
    direct_rows = [row for row in rows if str(row.get("subjectRelevance") or row.get("subject_relevance") or "").lower() in {"primary", "secondary"}]
    mention_count = sum(1 for row in rows if str(row.get("subjectRelevance") or row.get("subject_relevance") or "").lower() == "mention")
    article_ids = canonical_article_ids(direct_rows)
    if not article_ids:
        return None

    redis_ttl_seconds = int(ttl_seconds if ttl_seconds is not None else os.getenv("NEWS_DAILY_REDIS_TTL_SECONDS", "2592000"))
    redis_max_items = int(max_items if max_items is not None else os.getenv("NEWS_DAILY_REDIS_MAX_ITEMS", "30"))
    digest = article_ids_hash(article_ids)
    existing = read_existing_daily_summary(clickhouse_client, symbol=symbol, date=date, locale=locale)
    if existing and str(existing.get("articleIdsHash") or existing.get("article_ids_hash") or "") == digest:
        existing_record = clickhouse_row_to_daily_summary(existing)
        if redis_client is not None and existing_record.get("date") and existing_record.get("symbol") and existing_record.get("summary"):
            write_company_daily_summary_to_redis(
                redis_client,
                existing_record,
                ttl_seconds=redis_ttl_seconds,
                max_items=redis_max_items,
                locale=locale,
            )
        return None

    intelligence, model_used = build_daily_summary_intelligence(
        symbol=symbol,
        date=date,
        rows=direct_rows,
        summarize_fn=summarize_fn,
        model=model,
    )
    record = build_daily_summary_record(
        symbol=symbol,
        date=date,
        rows=direct_rows,
        locale=locale,
        model=model_used,
        generated_at=utc_now_iso(),
        status=event.get("status") or daily_summary_status(date),
        mention_count=mention_count,
        **intelligence,
    )
    row = daily_summary_to_clickhouse_row(record)
    clickhouse_client.insert_json_each_row("news_company_daily_summaries", [row])
    write_company_daily_summary_to_redis(
        redis_client,
        record,
        ttl_seconds=redis_ttl_seconds,
        max_items=redis_max_items,
        locale=locale,
    )
    return record


def read_daily_candidate_rows(client, *, symbol, date, locale):
    query = f"""
    SELECT
      toString(toDate(published_at)) AS date,
      formatDateTime(published_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS publishedAt,
      symbol,
      article_id AS articleId,
      locale,
      symbols,
      target_symbol AS targetSymbol,
      subject_relevance AS subjectRelevance,
      relevance_score_v2 AS relevanceScoreV2,
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
      why_it_matters AS whyItMatters
    FROM {client.database}.news_article_localizations
    WHERE target_symbol = {{symbol:String}}
      AND locale = {{locale:String}}
      AND toDate(published_at) = toDate({{date:String}})
      AND subject_relevance IN ['primary', 'secondary', 'mention']
    ORDER BY subject_relevance ASC, relevance_score_v2 DESC, published_at DESC
    LIMIT {{limit:UInt32}}
    FORMAT JSONEachRow
    """
    return client.query_json_each_row(query, {"symbol": symbol, "date": date, "locale": locale, "limit": int(os.getenv("NEWS_DAILY_SUMMARY_ARTICLE_LIMIT", "50"))})


def read_existing_daily_summary(client, *, symbol, date, locale):
    query = f"""
    SELECT
      toString(summaries.date) AS date,
      summaries.symbol,
      summaries.locale,
      summaries.summary,
      summaries.key_points AS keyPoints,
      summaries.positive_points AS positivePoints,
      summaries.concerns,
      summaries.impact_direction AS impactDirection,
      summaries.sentiment,
      summaries.article_ids AS articleIds,
      summaries.article_ids_hash AS articleIdsHash,
      summaries.article_count AS articleCount,
      summaries.mention_count AS mentionCount,
      summaries.status,
      summaries.model,
      formatDateTime(summaries.generated_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS generatedAt,
      summaries.version,
      summaries.raw
    FROM {client.database}.news_company_daily_summaries AS summaries
    WHERE summaries.symbol = {{symbol:String}}
      AND summaries.locale = {{locale:String}}
      AND summaries.date = {{date:Date}}
    ORDER BY summaries.generated_at DESC
    LIMIT 1
    FORMAT JSONEachRow
    """
    rows = client.query_json_each_row(query, {"symbol": symbol, "date": date, "locale": locale})
    return rows[0] if rows else None


def build_daily_summary_intelligence(*, symbol, date, rows, summarize_fn=None, model=None):
    if summarize_fn is not None:
        summary = summarize_fn(symbol=symbol, date=date, rows=rows)
        return normalize_summary_intelligence(summary), model or "custom"

    provider = os.getenv("NEWS_DAILY_SUMMARY_PROVIDER", "openai" if os.getenv("OPENAI_API_KEY") else "deterministic").strip().lower()
    if provider == "openai":
        try:
            enriched = summarize_daily_with_openai(symbol=symbol, date=date, rows=rows, model=model)
            if enriched:
                return normalize_summary_intelligence(enriched), model or os.getenv("NEWS_DAILY_SUMMARY_MODEL", "openai")
        except Exception as exc:
            print(f"News daily summary OpenAI 실패: symbol={symbol} date={date} error={exc.__class__.__name__}", file=sys.stderr, flush=True)
    return {}, "deterministic"


def summarize_daily_with_openai(*, symbol, date, rows, model=None):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": model or os.getenv("NEWS_DAILY_SUMMARY_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini")),
        "input": [
            {
                "role": "system",
                "content": (
                    "You prepare a daily company news brief for Korean retail investors. "
                    "Use only supplied article summaries. Do not add investment advice or outside facts. "
                    "Return concise Korean JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "symbol": symbol,
                        "date": date,
                        "articles": [
                            {
                                "headline": row.get("localizedHeadline") or row.get("headline"),
                                "summary": row.get("localizedSummary") or row.get("summary"),
                                "eventType": row.get("eventType") or row.get("event_type"),
                                "impactDirection": row.get("impactDirection") or row.get("impact_direction"),
                            }
                            for row in rows[:20]
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "news_company_daily_summary",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string"},
                        "keyPoints": {"type": "array", "items": {"type": "string"}},
                        "positivePoints": {"type": "array", "items": {"type": "string"}},
                        "concerns": {"type": "array", "items": {"type": "string"}},
                        "impactDirection": {"type": "string", "enum": ["positive", "negative", "neutral", "mixed"]},
                        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral", "mixed"]},
                    },
                    "required": ["summary", "keyPoints", "positivePoints", "concerns", "impactDirection", "sentiment"],
                },
            }
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(os.getenv("NEWS_DAILY_SUMMARY_TIMEOUT_SECONDS", "20"))) as response:
        data = json.loads(response.read().decode("utf-8"))
    return parse_openai_json(data)


def parse_openai_json(data):
    output_text = data.get("output_text") if isinstance(data, dict) else None
    if isinstance(output_text, str) and output_text.strip():
        try:
            decoded = json.loads(output_text)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            pass
    for item in data.get("output", []) if isinstance(data, dict) else []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_json" and isinstance(content.get("json"), dict):
                return content["json"]
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    decoded = json.loads(text)
                    if isinstance(decoded, dict):
                        return decoded
                except Exception:
                    continue
    return None


def normalize_summary_intelligence(value):
    if not isinstance(value, dict):
        return {}
    return {
        "summary": value.get("summary"),
        "key_points": value.get("keyPoints") or value.get("key_points"),
        "positive_points": value.get("positivePoints") or value.get("positive_points"),
        "concerns": value.get("concerns"),
        "impact_direction": value.get("impactDirection") or value.get("impact_direction"),
        "sentiment": value.get("sentiment"),
    }


def daily_summary_status(date):
    try:
        day = datetime.fromisoformat(str(date)).date()
    except Exception:
        return "rolling"
    return "final" if day < datetime.now(timezone.utc).date() else "rolling"


def bool_env(name, default):
    if isinstance(name, bool):
        return name
    value = os.getenv(name) if isinstance(name, str) and name.isupper() else name
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def int_env(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


if __name__ == "__main__":
    main()
