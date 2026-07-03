# 역할: Alpaca News API를 주기적으로 읽어 Kafka processed news topic에 발행합니다.
# 사용: Docker/EKS에서 alpaca-news-ingestor pod로 실행합니다.
import os
import sys
import time

from alfaka.alpaca.news import build_news_events, fetch_alpaca_news
from alfaka.common.env import load_dotenv, parse_csv, utc_now_iso
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.secrets import load_alpaca_credentials


def main():
    load_dotenv()
    key_id, secret_key = load_alpaca_credentials()
    if not key_id or not secret_key:
        print("Alpaca 키가 없습니다. 뉴스 수집기를 시작할 수 없습니다.", file=sys.stderr, flush=True)
        while True:
            time.sleep(float(os.getenv("ALPACA_NEWS_CREDENTIAL_RETRY_SECONDS", "300")))

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_NEWS_TOPIC", "market.news.alpaca.v1")
    symbols = parse_csv(os.getenv("ALPACA_SYMBOLS", ""))
    limit = int(os.getenv("ALPACA_NEWS_LIMIT", "50"))
    poll_seconds = float(os.getenv("ALPACA_NEWS_POLL_SECONDS", "300"))
    include_content = os.getenv("ALPACA_NEWS_INCLUDE_CONTENT", "false").lower() in {"1", "true", "yes"}
    producer = create_json_producer(kafka_servers, "alfaka-alpaca-news-ingestor")
    seen_ids = set()

    print(f"Alpaca 뉴스 수집 시작: symbols={symbols} topic={topic}", flush=True)
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


if __name__ == "__main__":
    main()
