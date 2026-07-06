# 역할: 수집된 Alpaca 뉴스에 한국어 요약/분류를 미리 붙여 ClickHouse와 Redis hot cache에 저장합니다.
# 사용: Kafka topic market.news.alpaca.v1을 consume하는 Docker/EKS worker로 실행합니다.
import json
import os
import sys
import urllib.request

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.kafka_io import create_json_consumer
from alfaka.serving.news_hot_cache import (
    DEFAULT_NEWS_MAX_ITEMS,
    DEFAULT_NEWS_RETENTION_DAYS,
    DEFAULT_NEWS_TTL_SECONDS,
    write_localized_news_to_redis,
)
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient
from alfaka.storage.news_intelligence import (
    build_news_intelligence_record,
    deterministic_news_intelligence,
    news_intelligence_to_clickhouse_row,
    utc_now_iso,
)


def main():
    load_dotenv()
    if not bool_env("NEWS_INTELLIGENCE_ENABLED", True):
        print("News intelligence worker disabled: NEWS_INTELLIGENCE_ENABLED=false", flush=True)
        return

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1")
    group_id = os.getenv("NEWS_INTELLIGENCE_GROUP_ID", "alfaka-news-intelligence-worker")
    enable_auto_commit = bool_env("NEWS_INTELLIGENCE_ENABLE_AUTO_COMMIT", False)
    clickhouse = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    clickhouse.ensure_market_data_schema()
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    daily_summary_producer = None
    if bool_env("NEWS_DAILY_SUMMARY_ENABLED", True):
        try:
            from alfaka.common.kafka_io import create_json_producer

            daily_summary_producer = create_json_producer(kafka_servers, "alfaka-news-intelligence-daily-summary")
        except Exception as exc:
            print(f"News daily summary dirty producer 비활성화: {exc.__class__.__name__}", file=sys.stderr, flush=True)
    consumer = create_json_consumer(
        [topic],
        kafka_servers,
        group_id,
        "alfaka-news-intelligence-consumer",
        enable_auto_commit=enable_auto_commit,
        max_poll_interval_ms=int_env("NEWS_INTELLIGENCE_MAX_POLL_INTERVAL_MS", 900000),
        max_poll_records=int_env("NEWS_INTELLIGENCE_MAX_POLL_RECORDS", 1),
    )
    print(f"News intelligence worker 시작: topic={topic} group={group_id}", flush=True)

    for record in consumer:
        try:
            process_news_event(
                record.value,
                clickhouse_client=clickhouse,
                redis_client=redis_client,
                daily_summary_producer=daily_summary_producer,
            )
            if not enable_auto_commit:
                consumer.commit()
        except Exception as exc:
            print(f"News intelligence 처리 실패: {exc}; payload={json.dumps(record.value, ensure_ascii=False)}", file=sys.stderr, flush=True)


def process_news_event(
    event,
    *,
    clickhouse_client,
    redis_client=None,
    enrich_fn=None,
    locale=None,
    model=None,
    ttl_seconds=None,
    max_items=None,
    daily_summary_producer=None,
):
    if not isinstance(event, dict) or event.get("eventType") != "NEWS_ARTICLE":
        return None

    locale = locale or os.getenv("NEWS_INTELLIGENCE_LOCALE", "ko-KR")
    model = model or os.getenv("NEWS_INTELLIGENCE_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    if is_already_processed(redis_client, event, locale):
        return None

    intelligence, model_used = build_intelligence(event, enrich_fn=enrich_fn, model=model)
    record = build_news_intelligence_record(
        event,
        intelligence,
        locale=locale,
        model=model_used,
        localized_at=utc_now_iso(),
    )
    row = news_intelligence_to_clickhouse_row(record)
    clickhouse_client.insert_json_each_row("news_article_localizations", [row])
    write_localized_news_to_redis(
        redis_client,
        record,
        ttl_seconds=int(ttl_seconds if ttl_seconds is not None else os.getenv("NEWS_REDIS_TTL_SECONDS", str(DEFAULT_NEWS_TTL_SECONDS))),
        max_items=int(max_items if max_items is not None else os.getenv("NEWS_REDIS_MAX_ITEMS", str(DEFAULT_NEWS_MAX_ITEMS))),
        retention_days=int(os.getenv("NEWS_REDIS_RETENTION_DAYS", str(DEFAULT_NEWS_RETENTION_DAYS))),
        locale=locale,
        topics=event_topics(event),
    )
    publish_daily_summary_dirty_event(record, producer=daily_summary_producer, redis_client=redis_client, locale=locale)
    mark_processed(redis_client, event, locale)
    return record


def build_intelligence(event, *, enrich_fn=None, model=None):
    if enrich_fn is not None:
        enriched = enrich_fn(event)
        return enriched or deterministic_news_intelligence(event), model or "custom"

    provider = os.getenv("NEWS_INTELLIGENCE_PROVIDER", "openai" if os.getenv("OPENAI_API_KEY") else "deterministic").strip().lower()
    if provider == "deterministic":
        return deterministic_news_intelligence(event), "deterministic"

    try:
        enriched = enrich_news_with_openai(event, model=model)
        if enriched:
            return enriched, model or os.getenv("NEWS_INTELLIGENCE_MODEL", "openai")
    except Exception as exc:
        print(f"News intelligence OpenAI 실패: article={event.get('articleId')} error={exc.__class__.__name__}", file=sys.stderr, flush=True)

    if bool_env("NEWS_INTELLIGENCE_DETERMINISTIC_FALLBACK", True):
        return deterministic_news_intelligence(event), "deterministic-fallback"
    raise RuntimeError("news intelligence enrichment failed")


def enrich_news_with_openai(event, *, model=None):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": model or os.getenv("NEWS_INTELLIGENCE_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini")),
        "input": [
            {
                "role": "system",
                "content": (
                    "You prepare US stock news for Korean retail investors. "
                    "Use only the supplied article fields. "
                    "Return concise Korean headline/summary plus neutral classification JSON. "
                    "Do not add recommendations or facts not present in the article."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "symbol": event.get("symbol"),
                        "symbols": event.get("symbols"),
                        "headline": event.get("headline"),
                        "summary": event.get("summary"),
                        "content": event.get("content"),
                        "source": event.get("source"),
                        "publishedAt": event.get("publishedAt"),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "news_intelligence",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "localizedHeadline": {"type": "string"},
                        "localizedSummary": {"type": "string"},
                        "keyPoints": {"type": "array", "items": {"type": "string"}},
                        "positivePoints": {"type": "array", "items": {"type": "string"}},
                        "concerns": {"type": "array", "items": {"type": "string"}},
                        "eventType": {"type": "string"},
                        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral", "mixed"]},
                        "impactDirection": {"type": "string", "enum": ["positive", "negative", "neutral", "mixed"]},
                        "whyItMatters": {"type": "string"},
                    },
                    "required": [
                        "localizedHeadline",
                        "localizedSummary",
                        "keyPoints",
                        "positivePoints",
                        "concerns",
                        "eventType",
                        "sentiment",
                        "impactDirection",
                        "whyItMatters",
                    ],
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
    with urllib.request.urlopen(request, timeout=float(os.getenv("NEWS_INTELLIGENCE_TIMEOUT_SECONDS", "8"))) as response:
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


def is_already_processed(redis_client, event, locale):
    if redis_client is None:
        return False
    try:
        return bool(redis_client.exists(processed_key(event, locale)))
    except Exception:
        return False


def mark_processed(redis_client, event, locale):
    if redis_client is None:
        return
    try:
        redis_client.set(processed_key(event, locale), "1", ex=int(os.getenv("NEWS_INTELLIGENCE_DEDUPE_TTL_SECONDS", "2592000")))
    except Exception:
        return


def processed_key(event, locale):
    symbol = str(event.get("symbol") or "UNKNOWN").upper()
    article_id = str(event.get("articleId") or event.get("sourceEventId") or "")
    return f"news:intelligence:processed:{locale}:{symbol}:{article_id}"


def event_topics(event):
    raw_topics = event.get("topics") or event.get("topic") or []
    if isinstance(raw_topics, str):
        raw_topics = [raw_topics]
    return [str(topic).strip() for topic in raw_topics if str(topic).strip()] if isinstance(raw_topics, list) else []


def publish_daily_summary_dirty_event(record, *, producer=None, redis_client=None, locale=None):
    if not bool_env("NEWS_DAILY_SUMMARY_ENABLED", True):
        return None
    symbol = str(record.get("targetSymbol") or record.get("symbol") or "").strip().upper()
    date = published_date(record.get("publishedAt"))
    if not symbol or not date:
        return None
    event = {
        "eventType": "NEWS_DAILY_SUMMARY_DIRTY",
        "symbol": symbol,
        "date": date,
        "locale": locale or record.get("locale") or os.getenv("NEWS_INTELLIGENCE_LOCALE", "ko-KR"),
        "articleId": record.get("articleId"),
        "source": "news-intelligence-worker",
        "createdAt": utc_now_iso(),
    }
    if producer is not None:
        topic = os.getenv("KAFKA_NEWS_DAILY_SUMMARY_DIRTY_TOPIC", "market.news.daily-summary-dirty.v1")
        producer.send(topic, key=f"{symbol}:{date}", value=event)
        return event
    if redis_client is not None and bool_env("NEWS_DAILY_SUMMARY_DIRTY_REDIS_FALLBACK", False):
        try:
            redis_client.lpush(os.getenv("NEWS_DAILY_SUMMARY_DIRTY_REDIS_KEY", "news:daily-summary:dirty"), json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            return event
        except Exception:
            return None
    return event


def published_date(value):
    if not value:
        return None
    try:
        return str(value)[:10]
    except Exception:
        return None


def bool_env(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_env(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


if __name__ == "__main__":
    main()
