# 역할: Alpaca News API를 주기적으로 읽어 Kafka processed news topic에 발행합니다.
# 사용: Docker/EKS에서 alpaca-news-ingestor pod로 실행합니다.
import os
import sys
import time

from alfaka.alpaca.news import build_news_events, fetch_alpaca_news, normalize_article_symbols
from alfaka.common.env import load_dotenv, parse_csv, utc_now_iso
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.secrets import load_alpaca_credentials
from alfaka.storage.news_s3_archive import upload_canonical_news_article_to_s3, write_news_symbol_index_to_s3


def main():
    load_dotenv()
    key_id, secret_key = load_alpaca_credentials()
    if not key_id or not secret_key:
        print("Alpaca 키가 없습니다. 뉴스 수집기를 시작할 수 없습니다.", file=sys.stderr, flush=True)
        while True:
            time.sleep(float(os.getenv("ALPACA_NEWS_CREDENTIAL_RETRY_SECONDS", "300")))

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1")
    symbols = parse_csv(os.getenv("ALPACA_SYMBOLS", "AAPL,NVDA,MSFT,TSLA,AMZN,META,GOOGL"))
    limit = int(os.getenv("ALPACA_NEWS_LIMIT", "50"))
    poll_seconds = float(os.getenv("ALPACA_NEWS_POLL_SECONDS", "300"))
    include_content = os.getenv("ALPACA_NEWS_INCLUDE_CONTENT", "false").lower() in {"1", "true", "yes"}
    producer = create_json_producer(kafka_servers, "alfaka-alpaca-news-ingestor")
    s3 = None
    s3_bucket = os.getenv("S3_BUCKET")
    s3_prefix = os.getenv("S3_RAW_PREFIX", "market-data/raw/alpaca")
    archive_to_s3 = os.getenv("NEWS_S3_ARCHIVE_ENABLED", "false").lower() in {"1", "true", "yes"} and bool(s3_bucket)
    if archive_to_s3:
        from alfaka.common.s3_client import create_s3_client

        s3 = create_s3_client()
    seen_ids = set()

    print(f"Alpaca 뉴스 수집 시작: symbols={symbols} topic={topic} s3Archive={archive_to_s3}", flush=True)
    while True:
        try:
            received_at = utc_now_iso()
            articles = fetch_alpaca_news(
                key_id,
                secret_key,
                symbols=symbols,
                limit=limit,
                include_content=include_content,
            )
            published = 0
            for article in articles:
                if not isinstance(article, dict):
                    continue
                if archive_to_s3:
                    archive_article_to_s3(s3, s3_bucket, s3_prefix, article, requested_symbols=symbols, received_at=received_at)
                for event in build_news_events(article, requested_symbols=symbols, received_at=received_at):
                    dedupe_key = f"{event['symbol']}:{event['articleId']}"
                    if dedupe_key in seen_ids:
                        continue
                    seen_ids.add(dedupe_key)
                    producer.send(topic, key=event["symbol"], value=event)
                    published += 1
            producer.flush(timeout=10)
            print(f"Alpaca 뉴스 적재: articles={len(articles)} events={published}", flush=True)
            if len(seen_ids) > 5000:
                seen_ids = set(list(seen_ids)[-2500:])
        except Exception as exc:
            print(f"Alpaca 뉴스 수집 실패: {exc}", file=sys.stderr, flush=True)
        time.sleep(poll_seconds)


def archive_article_to_s3(s3, bucket, prefix, article, *, requested_symbols, received_at):
    try:
        raw_result = upload_canonical_news_article_to_s3(
            s3,
            bucket,
            prefix,
            article,
            received_at=received_at,
        )
        requested_list = [symbol.upper() for symbol in requested_symbols or []]
        requested = set(requested_list)
        article_symbols = [symbol for symbol in normalize_article_symbols(article) if not requested or symbol in requested]
        index_symbols = article_symbols or requested_list[:1]
        for symbol in index_symbols:
            write_news_symbol_index_to_s3(
                s3,
                bucket,
                prefix,
                article,
                symbol=symbol,
                canonical_key=raw_result["key"],
                received_at=received_at,
            )
    except Exception as exc:
        print(f"Alpaca 뉴스 S3 archive 실패: article={article.get('id') or article.get('articleId')} error={exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
