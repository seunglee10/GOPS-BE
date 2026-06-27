# 역할: Alpaca WebSocket에서 실시간 데이터를 받아 Kafka Raw Topic에 저장합니다.
# 사용: 결제 후 ALPACA_FEED=sip과 API 키를 넣고 실행하면 실제 데이터가 Kafka로 들어갑니다.
# 출력: market.raw.bars, market.raw.updated-bars, market.raw.trades.
import asyncio
import json
import os
import sys

import websockets

from alfaka.alpaca.subscription import build_subscription_request, load_symbols_and_channels
from alfaka.common.env import load_dotenv
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.market_messages import CONTROL_MESSAGE_TYPES, build_raw_envelope, raw_topic_name
from alfaka.common.secrets import load_alpaca_credentials


async def main():
    load_dotenv()

    alpaca_key, alpaca_secret = load_alpaca_credentials()
    alpaca_feed = os.getenv("ALPACA_FEED", "sip")
    symbols, channels = load_symbols_and_channels()

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    kafka_client_id = os.getenv("KAFKA_CLIENT_ID", "alfaka-alpaca-ingestor")
    raw_topic_prefix = os.getenv("KAFKA_RAW_TOPIC_PREFIX", os.getenv("KAFKA_TOPIC_PREFIX", "market.raw"))

    if not alpaca_key or not alpaca_secret:
        print("Alpaca 키가 없습니다. .env 직접 키 또는 AWS Secrets Manager 설정을 넣어주세요.", file=sys.stderr)
        sys.exit(1)

    alpaca_url = "wss://stream.data.alpaca.markets/v2/test" if alpaca_feed == "test" else f"wss://stream.data.alpaca.markets/v2/{alpaca_feed}"
    producer = create_json_producer(kafka_servers, kafka_client_id)
    subscribe_request = build_subscription_request(symbols, channels)

    print(f"Alpaca 연결: {alpaca_url}", flush=True)
    print(f"요청 종목: {symbols}", flush=True)
    print(f"요청 채널: {channels}", flush=True)
    print(f"Kafka Raw Topic Prefix: {raw_topic_prefix}", flush=True)

    async with websockets.connect(alpaca_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"action": "auth", "key": alpaca_key, "secret": alpaca_secret}))

        while True:
            raw_frame = await ws.recv()
            messages = json.loads(raw_frame)

            for message in messages:
                message_type = message.get("T")

                if message_type == "success":
                    print(message, flush=True)
                    if message.get("msg") == "authenticated":
                        print("구독 요청:", subscribe_request, flush=True)
                        await ws.send(json.dumps(subscribe_request))
                    continue

                if message_type == "subscription":
                    print("현재 구독:", message, flush=True)
                    continue

                if message_type == "error":
                    print("Alpaca 에러:", message, file=sys.stderr, flush=True)
                    continue

                if message_type in CONTROL_MESSAGE_TYPES:
                    continue

                envelope = build_raw_envelope(message=message, feed=alpaca_feed)
                kafka_topic = raw_topic_name(raw_topic_prefix, message_type)
                kafka_key = envelope["symbol"]
                producer.send(kafka_topic, key=kafka_key, value=envelope)
                print(f"Kafka Raw 전송: topic={kafka_topic}, key={kafka_key}, channel={envelope['channel']}", flush=True)


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Alpaca 수집기를 종료합니다.")
