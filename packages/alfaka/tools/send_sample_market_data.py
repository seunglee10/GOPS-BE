# 역할: Alpaca 없이도 Kafka Raw Topic에 샘플 시장 데이터를 넣습니다.
# 사용: 로컬 Docker 파이프라인의 Kafka->Redis->S3 흐름을 검증합니다.
# 출력: market.raw.trades, market.raw.bars, market.raw.updated-bars, market.raw.daily-bars, market.raw.statuses.
import argparse
import os
from datetime import datetime, timedelta, timezone

from alfaka.common.env import load_dotenv
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.market_messages import build_raw_envelope, raw_topic_name


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def send(producer, raw_prefix, feed, message):
    topic = raw_topic_name(raw_prefix, message["T"])
    envelope = build_raw_envelope(message=message, feed=feed)
    producer.send(topic, key=envelope["symbol"], value=envelope)
    print(f"sample 전송: topic={topic}, key={envelope['symbol']}, channel={envelope['channel']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="로컬 Kafka Raw Topic에 샘플 Alpaca 데이터를 넣습니다.")
    parser.add_argument("symbol", nargs="?", default="AAPL", help="샘플 심볼입니다. 예: AAPL")
    args = parser.parse_args()

    load_dotenv()
    symbol = args.symbol.upper()
    feed = os.getenv("ALPACA_FEED", "sip")
    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    raw_prefix = os.getenv("KAFKA_RAW_TOPIC_PREFIX", "market.raw")
    producer = create_json_producer(kafka_servers, "alfaka-local-sample-producer")
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)

    for i in range(3):
        price = round(195.00 + i * 0.13, 2)
        send(producer, raw_prefix, feed, {"T": "t", "S": symbol, "i": 900000 + i, "p": price, "s": 10 + i, "x": "D", "c": ["@"], "z": "C", "t": iso(base + timedelta(minutes=10, seconds=i * 10))})

    for i in range(10):
        open_price = round(194.50 + i * 0.08, 2)
        close_price = round(open_price + 0.05, 2)
        send(producer, raw_prefix, feed, {"T": "b", "S": symbol, "t": iso(base + timedelta(minutes=i)), "o": open_price, "h": round(open_price + 0.18, 2), "l": round(open_price - 0.12, 2), "c": close_price, "v": 1000 + i * 25, "n": 100 + i, "vw": round((open_price + close_price) / 2, 2)})

    send(producer, raw_prefix, feed, {"T": "u", "S": symbol, "t": iso(base + timedelta(minutes=2)), "o": 194.66, "h": 194.95, "l": 194.50, "c": 194.83, "v": 1200, "n": 130, "vw": 194.74})
    send(producer, raw_prefix, feed, {"T": "d", "S": symbol, "t": iso(base.replace(hour=0, minute=0)), "o": 193.50, "h": 196.10, "l": 192.80, "c": 195.40, "v": 850000, "n": 45210, "vw": 194.80})
    send(producer, raw_prefix, feed, {"T": "s", "S": symbol, "t": iso(base), "sc": "active", "st": "trading"})
    producer.flush(10)
    print("샘플 데이터 전송 완료", flush=True)


if __name__ == "__main__":
    main()
