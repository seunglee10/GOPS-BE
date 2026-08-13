# 역할: Alpaca News API에서 과거 뉴스를 받아 S3 canonical raw와 최근 30일 Kafka 처리 경로를 채웁니다.
# 사용: AWS/EKS Job 또는 로컬 jobs profile에서 1회성 뉴스 백필로 실행합니다.
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data.alpaca.news import build_news_event, iter_alpaca_news_pages
from market_data.common.env import load_dotenv, parse_csv, utc_now_iso
from market_data.common.kafka_io import create_json_producer
from market_data.common.secrets import load_alpaca_credentials
from market_data.storage.news_s3_archive import (
    article_published_at,
    news_backfill_chunk_marker_key,
    s3_object_exists,
    upload_canonical_news_article_to_s3,
    write_news_backfill_chunk_marker,
    write_news_symbol_index_to_s3,
)


def main():
    load_dotenv()
    config = news_backfill_runtime_config()
    if config["dryRun"]:
        result = plan_news_backfill(config)
        print(f"News backfill dry-run: {json.dumps(result, ensure_ascii=False, sort_keys=True)}", flush=True)
        return

    key_id, secret_key = load_alpaca_credentials()
    if not key_id or not secret_key:
        raise SystemExit("Alpaca credentials are required for news backfill.")
    if not config["s3Bucket"]:
        raise SystemExit("S3_BUCKET is required for news backfill.")

    from market_data.common.s3_client import create_s3_client

    s3 = create_s3_client()
    producer = None
    if config["publishRecentToKafka"]:
        producer = create_json_producer(config["kafkaBootstrapServers"], "alfaka-news-backfill")

    result = run_news_backfill(
        config,
        s3=s3,
        producer=producer,
        key_id=key_id,
        secret_key=secret_key,
    )
    print(f"News backfill 완료: {json.dumps(result, ensure_ascii=False, sort_keys=True)}", flush=True)


def news_backfill_runtime_config(environ=None, now=None):
    environ = environ or os.environ
    now = now or datetime.now(timezone.utc)
    explicit_end = environ.get("NEWS_BACKFILL_END")
    end_dt = parse_backfill_time(explicit_end) if explicit_end else now
    explicit_start = environ.get("NEWS_BACKFILL_START")
    days = int(environ.get("NEWS_BACKFILL_DAYS", "365"))
    start_dt = parse_backfill_time(explicit_start) if explicit_start else end_dt - timedelta(days=days)
    symbols = resolve_news_backfill_symbols(environ)
    max_symbols = int(environ.get("NEWS_BACKFILL_MAX_SYMBOLS", "0") or 0)
    if max_symbols > 0:
        symbols = symbols[:max_symbols]
    shard_count = max(1, int(environ.get("NEWS_BACKFILL_SHARD_COUNT", "1") or 1))
    shard_index = int(environ.get("NEWS_BACKFILL_SHARD_INDEX", "0") or 0)
    symbols = select_symbol_shard(symbols, shard_index, shard_count)
    return {
        "symbols": symbols,
        "shardIndex": shard_index,
        "shardCount": shard_count,
        "start": iso_z(start_dt),
        "end": iso_z(end_dt),
        "chunkDays": max(1, int(environ.get("NEWS_BACKFILL_CHUNK_DAYS", "7"))),
        "clickhouseDays": max(0, int(environ.get("NEWS_CLICKHOUSE_DAYS", "30"))),
        "limit": max(1, min(50, int(environ.get("NEWS_BACKFILL_LIMIT", environ.get("ALPACA_NEWS_LIMIT", "50"))))),
        "includeContent": bool_env(environ.get("NEWS_BACKFILL_INCLUDE_CONTENT"), default=True),
        "sort": environ.get("NEWS_BACKFILL_SORT", "asc"),
        "s3Bucket": environ.get("S3_BUCKET"),
        "s3RawPrefix": environ.get("S3_RAW_PREFIX", "market-data/raw/alpaca"),
        "force": bool_env(environ.get("NEWS_BACKFILL_FORCE"), default=False),
        "skipCompletedChunks": bool_env(environ.get("NEWS_BACKFILL_SKIP_COMPLETED_CHUNKS"), default=True),
        "sleepSeconds": max(0.0, float(environ.get("NEWS_BACKFILL_SLEEP_SECONDS", "0.2"))),
        "maxPagesPerChunk": int(environ.get("NEWS_BACKFILL_MAX_PAGES_PER_CHUNK", "0") or 0),
        "maxChunks": int(environ.get("NEWS_BACKFILL_MAX_CHUNKS", "0") or 0),
        "publishRecentToKafka": bool_env(environ.get("NEWS_BACKFILL_PUBLISH_RECENT_TO_KAFKA"), default=True),
        "kafkaBootstrapServers": environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        "kafkaNewsTopic": environ.get("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1"),
        "dryRun": bool_env(environ.get("NEWS_BACKFILL_DRY_RUN"), default=False),
    }


def resolve_news_backfill_symbols(environ):
    explicit = parse_csv(environ.get("NEWS_BACKFILL_SYMBOLS", ""))
    if explicit:
        return normalize_symbols(explicit)
    universe = (environ.get("NEWS_BACKFILL_UNIVERSE") or environ.get("ALPACA_UNIVERSE") or "sp500").strip().lower()
    if universe == "sp500":
        path = (
            str(environ.get("NEWS_BACKFILL_UNIVERSE_REGISTRY_PATH") or "").strip()
            or str(environ.get("ALPACA_UNIVERSE_REGISTRY_PATH") or "").strip()
            or "systems/market-data/config/sp500-universe.json"
        )
        values = load_symbols_from_registry(path)
        if values:
            return normalize_symbols(values)
    fallback = parse_csv(environ.get("ALPACA_SYMBOLS", "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA"))
    return normalize_symbols(fallback)


def run_news_backfill(config, *, s3, producer=None, key_id=None, secret_key=None, fetch_pages_fn=None):
    fetch_pages_fn = fetch_pages_fn or iter_alpaca_news_pages
    cutoff = parse_backfill_time(config["end"]) - timedelta(days=int(config["clickhouseDays"]))
    stats = {
        "symbols": len(config["symbols"]),
        "chunks": 0,
        "chunksSkipped": 0,
        "pages": 0,
        "articlesSeen": 0,
        "s3RawStored": 0,
        "s3RawSkipped": 0,
        "s3IndexStored": 0,
        "s3IndexSkipped": 0,
        "eventsPublished": 0,
        "articlesOutOfRange": 0,
    }
    published_event_keys = set()
    max_chunks = int(config.get("maxChunks") or 0)

    for symbol in config["symbols"]:
        for start, end in iter_time_chunks(config["start"], config["end"], config["chunkDays"]):
            if max_chunks and stats["chunks"] >= max_chunks:
                flush_producer(producer)
                return stats
            marker_key = news_backfill_chunk_marker_key(config["s3RawPrefix"], symbol, start, end)
            if config.get("skipCompletedChunks", True) and not config.get("force") and s3_object_exists(s3, config["s3Bucket"], marker_key):
                stats["chunksSkipped"] += 1
                continue
            stats["chunks"] += 1
            chunk_stats = process_news_backfill_chunk(
                config,
                s3=s3,
                producer=producer,
                key_id=key_id,
                secret_key=secret_key,
                fetch_pages_fn=fetch_pages_fn,
                symbol=symbol,
                start=start,
                end=end,
                cutoff=cutoff,
                published_event_keys=published_event_keys,
            )
            merge_stats(stats, chunk_stats)
            write_news_backfill_chunk_marker(
                s3,
                config["s3Bucket"],
                config["s3RawPrefix"],
                symbol,
                start,
                end,
                chunk_stats,
                force=config.get("force", False),
            )
            if config.get("sleepSeconds", 0) > 0:
                time.sleep(config["sleepSeconds"])

    flush_producer(producer)
    return stats


def plan_news_backfill(config):
    chunk_count = sum(1 for _start, _end in iter_time_chunks(config["start"], config["end"], config["chunkDays"]))
    symbol_count = len(config["symbols"])
    total_chunks = symbol_count * chunk_count
    return {
        "dryRun": True,
        "symbols": symbol_count,
        "symbolSample": config["symbols"][:10],
        "shardIndex": config.get("shardIndex", 0),
        "shardCount": config.get("shardCount", 1),
        "start": config["start"],
        "end": config["end"],
        "chunkDays": config["chunkDays"],
        "chunksPerSymbol": chunk_count,
        "totalChunks": total_chunks,
        "s3Bucket": config.get("s3Bucket"),
        "s3RawPrefix": config.get("s3RawPrefix"),
        "includeContent": config.get("includeContent"),
        "publishRecentToKafka": config.get("publishRecentToKafka"),
        "clickhouseDays": config.get("clickhouseDays"),
        "nextStep": "Set NEWS_BACKFILL_DRY_RUN=false only for the reviewed one-shot backfill run.",
    }


def process_news_backfill_chunk(
    config,
    *,
    s3,
    producer,
    key_id,
    secret_key,
    fetch_pages_fn,
    symbol,
    start,
    end,
    cutoff,
    published_event_keys,
):
    stats = {
        "pages": 0,
        "articlesSeen": 0,
        "s3RawStored": 0,
        "s3RawSkipped": 0,
        "s3IndexStored": 0,
        "s3IndexSkipped": 0,
        "eventsPublished": 0,
        "articlesOutOfRange": 0,
    }
    pages = fetch_pages_fn(
        key_id,
        secret_key,
        symbols=[symbol],
        limit=config["limit"],
        include_content=config["includeContent"],
        start=start,
        end=end,
        sort=config["sort"],
        max_pages=config["maxPagesPerChunk"] or None,
    )
    received_at = utc_now_iso()
    for page in pages:
        stats["pages"] += 1
        for article in page.get("news", []):
            if not isinstance(article, dict):
                continue
            stats["articlesSeen"] += 1
            if not is_article_in_range(article, start, end):
                stats["articlesOutOfRange"] += 1
                continue
            raw_result = upload_canonical_news_article_to_s3(
                s3,
                config["s3Bucket"],
                config["s3RawPrefix"],
                article,
                force=config.get("force", False),
                received_at=received_at,
            )
            stats["s3RawStored" if raw_result["stored"] else "s3RawSkipped"] += 1
            index_result = write_news_symbol_index_to_s3(
                s3,
                config["s3Bucket"],
                config["s3RawPrefix"],
                article,
                symbol=symbol,
                canonical_key=raw_result["key"],
                force=config.get("force", False),
                received_at=received_at,
            )
            stats["s3IndexStored" if index_result["stored"] else "s3IndexSkipped"] += 1
            if producer is not None and is_recent_article(article, cutoff):
                event = build_news_event(article, symbol=symbol, received_at=received_at)
                event_key = f"{event['symbol']}:{event['articleId']}"
                if event_key in published_event_keys:
                    continue
                published_event_keys.add(event_key)
                producer.send(config["kafkaNewsTopic"], key=event["symbol"], value=event)
                stats["eventsPublished"] += 1
        if config.get("sleepSeconds", 0) > 0:
            time.sleep(config["sleepSeconds"])
    return stats


def iter_time_chunks(start, end, chunk_days):
    cursor = parse_backfill_time(start)
    end_dt = parse_backfill_time(end)
    step = timedelta(days=max(1, int(chunk_days)))
    while cursor < end_dt:
        chunk_end = min(cursor + step, end_dt)
        yield iso_z(cursor), iso_z(chunk_end)
        cursor = chunk_end


def is_recent_article(article, cutoff):
    published_at = article_published_at(article)
    if not published_at:
        return False
    return parse_backfill_time(published_at) >= cutoff


def is_article_in_range(article, start, end):
    published_at = article_published_at(article)
    if not published_at:
        return False
    published = parse_backfill_time(published_at)
    return parse_backfill_time(start) <= published <= parse_backfill_time(end)


def merge_stats(total, increment):
    for key, value in increment.items():
        total[key] = int(total.get(key, 0)) + int(value or 0)


def flush_producer(producer):
    if producer is not None and hasattr(producer, "flush"):
        try:
            producer.flush(timeout=10)
        except TypeError:
            producer.flush()


def load_symbols_from_registry(path):
    registry_path = Path(path)
    if not registry_path.is_absolute():
        registry_path = Path.cwd() / registry_path
    if not registry_path.exists():
        repo_relative = Path(__file__).resolve().parents[4] / path
        if repo_relative.exists():
            registry_path = repo_relative
    if not registry_path.exists():
        return []
    if registry_path.is_dir():
        return []
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    values = payload.get("symbols") if isinstance(payload, dict) else None
    return values if isinstance(values, list) else []


def normalize_symbols(values):
    result = []
    seen = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            result.append(symbol)
            seen.add(symbol)
    return result


def select_symbol_shard(symbols, shard_index=0, shard_count=1):
    count = int(shard_count or 1)
    index = int(shard_index or 0)
    if count < 1:
        raise ValueError("NEWS_BACKFILL_SHARD_COUNT must be >= 1")
    if index < 0 or index >= count:
        raise ValueError("NEWS_BACKFILL_SHARD_INDEX must be between 0 and NEWS_BACKFILL_SHARD_COUNT - 1")
    if count == 1:
        return list(symbols)
    return [symbol for position, symbol in enumerate(symbols) if position % count == index]


def parse_backfill_time(value):
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    if len(text) == 10:
        text = f"{text}T00:00:00Z"
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_z(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bool_env(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
