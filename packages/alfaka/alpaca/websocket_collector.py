# 역할: Alpaca WebSocket에서 실시간 데이터를 받아 Kafka Raw Topic에 저장합니다.
# 사용: 결제 후 ALPACA_FEED=sip과 API 키를 넣고 실행하면 실제 데이터가 Kafka로 들어갑니다.
# 출력: market.raw.bars, market.raw.updated-bars, market.raw.trades.
import asyncio
import json
import os
import sys
import time

import redis
import websockets

from alfaka.alpaca.subscription import build_subscription_request, load_request_config, load_symbols_and_channels, validate_channels
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.market_messages import CONTROL_MESSAGE_TYPES, build_raw_envelope, raw_topic_name
from alfaka.common.secrets import load_alpaca_credentials


async def main():
    load_dotenv()

    alpaca_key, alpaca_secret = load_alpaca_credentials()
    alpaca_feed = os.getenv("ALPACA_FEED", "sip")
    symbols, channels = load_symbols_and_channels()
    request_config = load_request_config()
    active_channels = parse_csv(os.getenv("ALPACA_ACTIVE_CHANNELS", ",".join(request_config.get("activeChartChannels", ["trades"]))))
    validate_channels(active_channels, request_config)
    active_channels = [channel for channel in active_channels if channel not in channels]
    active_poll_seconds = parse_positive_float(os.getenv("ALPACA_ACTIVE_POLL_SECONDS", "5"), default=5.0)

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    kafka_client_id = os.getenv("KAFKA_CLIENT_ID", "alfaka-alpaca-ingestor")
    raw_topic_prefix = os.getenv("KAFKA_RAW_TOPIC_PREFIX", os.getenv("KAFKA_TOPIC_PREFIX", "market.raw"))

    if not alpaca_key or not alpaca_secret:
        print("Alpaca 키가 없습니다. .env 직접 키 또는 AWS Secrets Manager 설정을 넣어주세요.", file=sys.stderr)
        sys.exit(1)

    alpaca_url = "wss://stream.data.alpaca.markets/v2/test" if alpaca_feed == "test" else f"wss://stream.data.alpaca.markets/v2/{alpaca_feed}"
    producer = create_json_producer(kafka_servers, kafka_client_id)
    subscribe_request = build_subscription_request(symbols, channels)
    redis_client = create_active_subscription_redis()
    active_subscribed_symbols = set()
    last_active_sync = 0.0

    print(f"Alpaca 연결: {alpaca_url}", flush=True)
    print(f"요청 종목: {symbols}", flush=True)
    print(f"요청 채널: {channels}", flush=True)
    print(f"활성 차트 tick 채널: {active_channels or 'disabled'}", flush=True)
    print(f"Kafka Raw Topic Prefix: {raw_topic_prefix}", flush=True)

    async with websockets.connect(alpaca_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"action": "auth", "key": alpaca_key, "secret": alpaca_secret}))

        while True:
            try:
                raw_frame = await asyncio.wait_for(ws.recv(), timeout=max(1.0, active_poll_seconds))
            except asyncio.TimeoutError:
                active_subscribed_symbols = await sync_active_chart_subscriptions(
                    ws,
                    redis_client,
                    active_channels,
                    active_subscribed_symbols,
                )
                last_active_sync = time.monotonic()
                continue

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

            if time.monotonic() - last_active_sync >= active_poll_seconds:
                active_subscribed_symbols = await sync_active_chart_subscriptions(
                    ws,
                    redis_client,
                    active_channels,
                    active_subscribed_symbols,
                )
                last_active_sync = time.monotonic()


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Alpaca 수집기를 종료합니다.")


def create_active_subscription_redis():
    redis_url = os.getenv("ALPACA_ACTIVE_REDIS_URL", os.getenv("REDIS_URL"))
    enabled = os.getenv("ALPACA_ACTIVE_TICK_SUBSCRIPTION", "true").lower() not in {"0", "false", "no"}
    if not redis_url or not enabled:
        return None
    return redis.from_url(redis_url, decode_responses=True)


async def sync_active_chart_subscriptions(ws, redis_client, channels, subscribed_symbols):
    if not redis_client or not channels:
        return subscribed_symbols

    desired_symbols = read_active_chart_symbols(redis_client)
    subscribe_symbols = sorted(desired_symbols - subscribed_symbols)
    unsubscribe_symbols = sorted(subscribed_symbols - desired_symbols)

    if subscribe_symbols:
        request = build_subscription_request(subscribe_symbols, channels)
        print(f"활성 차트 구독 추가: {request}", flush=True)
        await ws.send(json.dumps(request))

    if unsubscribe_symbols:
        request = {"action": "unsubscribe"}
        for channel in channels:
            request[channel] = unsubscribe_symbols
        print(f"활성 차트 구독 해제: {request}", flush=True)
        await ws.send(json.dumps(request))

    return desired_symbols


def read_active_chart_symbols(redis_client):
    symbols = set()
    for symbol in redis_client.smembers("active:charts:symbols"):
        if redis_client.exists(f"active:charts:{symbol}"):
            symbols.add(symbol)
    return symbols


def parse_positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
