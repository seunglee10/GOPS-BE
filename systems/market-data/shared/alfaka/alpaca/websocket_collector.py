# 역할: Alpaca WebSocket에서 실시간 데이터를 받아 Kafka Raw Topic에 저장합니다.
# 사용: ALPACA_FEED_PROFILE 또는 legacy ALPACA_FEED를 설정하면 해당 feed runtime이 Kafka Raw Topic에 적재합니다.
# 출력: market.raw.bars, market.raw.updated-bars, market.raw.trades.
import asyncio
import json
import os
import sys
import time

import redis
import websockets

from alfaka.alpaca.feed_profiles import resolve_feed_profile
from alfaka.alpaca.subscription import build_subscription_request, load_request_config, load_symbols_and_channels, validate_channels
from alfaka.alpaca.trade_tiers import resolve_trade_subscription_plan
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.market_messages import CONTROL_MESSAGE_TYPES, build_raw_envelope, raw_topic_name
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.runtime_health import write_component_health
from alfaka.common.runtime_config import validate_required_values
from alfaka.common.secrets import load_alpaca_credentials


class AlpacaConnectionLimitError(RuntimeError):
    pass


async def main():
    load_dotenv()

    alpaca_key, alpaca_secret = load_alpaca_credentials()
    feed_profile = resolve_feed_profile()
    alpaca_feed = feed_profile.feed
    symbols, channels = load_symbols_and_channels()
    request_config = load_request_config()
    active_channels = parse_csv(os.getenv("ALPACA_ACTIVE_CHANNELS", ",".join(request_config.get("activeChartChannels", ["trades"]))))
    validate_channels(active_channels, request_config)
    active_channels = [channel for channel in active_channels if channel not in channels]
    active_poll_seconds = parse_positive_float(os.getenv("ALPACA_ACTIVE_POLL_SECONDS", "1"), default=1.0)

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    kafka_client_id = os.getenv("KAFKA_CLIENT_ID", "alfaka-alpaca-ingestor")
    raw_topic_prefix = os.getenv("KAFKA_RAW_TOPIC_PREFIX", os.getenv("KAFKA_TOPIC_PREFIX", "market.raw"))
    validate_required_values("alpaca ingestor", {
        "kafka_servers": kafka_servers,
        "raw_topic_prefix": raw_topic_prefix,
    })

    if not alpaca_key or not alpaca_secret:
        print("Alpaca 키가 없습니다. .env 직접 키 또는 AWS Secrets Manager 설정을 넣어주세요.", file=sys.stderr)
        sys.exit(1)

    alpaca_url = feed_profile.websocket_url
    producer = create_json_producer(kafka_servers, kafka_client_id)
    subscribe_request = build_subscription_request(symbols, channels)
    redis_client = create_active_subscription_redis()
    reconnect_backoff = parse_positive_float(os.getenv("ALPACA_RECONNECT_BACKOFF_SECONDS", "2"), default=2.0)
    reconnect_backoff_max = parse_positive_float(os.getenv("ALPACA_RECONNECT_BACKOFF_MAX_SECONDS", "60"), default=60.0)

    write_ingestor_health(
        redis_client,
        feed_profile,
        status="starting",
        alpacaFeed=alpaca_feed,
        websocketUrl=alpaca_url,
        channels=channels,
        symbolCount=len(symbols),
    )

    print(f"Alpaca profile: {feed_profile.profile_id} feed={alpaca_feed} sessions={','.join(feed_profile.sessions)}", flush=True)
    print(f"Alpaca 연결: {alpaca_url}", flush=True)
    print(f"요청 종목: {symbols}", flush=True)
    print(f"요청 채널: {channels}", flush=True)
    print(f"활성 차트 tick 채널: {active_channels or 'disabled'}", flush=True)
    print(f"Kafka Raw Topic Prefix: {raw_topic_prefix}", flush=True)

    delay = reconnect_backoff
    while True:
        try:
            await run_stream_session(
                alpaca_url=alpaca_url,
                alpaca_key=alpaca_key,
                alpaca_secret=alpaca_secret,
                alpaca_feed=alpaca_feed,
                feed_profile=feed_profile,
                producer=producer,
                subscribe_request=subscribe_request,
                redis_client=redis_client,
                active_channels=active_channels,
                active_poll_seconds=active_poll_seconds,
                raw_topic_prefix=raw_topic_prefix,
            )
            delay = reconnect_backoff
        except asyncio.CancelledError:
            raise
        except AlpacaConnectionLimitError as exc:
            delay = reconnect_backoff_max
            write_ingestor_health(
                redis_client,
                feed_profile,
                status="connection_limited",
                alpacaFeed=alpaca_feed,
                websocketUrl=alpaca_url,
                error=str(exc),
                retryDelaySeconds=delay,
            )
            print(f"Alpaca 연결 제한: error={exc}, delay={delay}s", file=sys.stderr, flush=True)
            await asyncio.sleep(delay)
        except Exception as exc:
            write_ingestor_health(
                redis_client,
                feed_profile,
                status="error",
                alpacaFeed=alpaca_feed,
                websocketUrl=alpaca_url,
                error=str(exc),
            )
            print(f"Alpaca 연결 재시도 예정: error={exc}, delay={delay}s", file=sys.stderr, flush=True)
            await asyncio.sleep(delay)
            delay = min(reconnect_backoff_max, delay * 2)


async def run_stream_session(
    *,
    alpaca_url,
    alpaca_key,
    alpaca_secret,
    alpaca_feed,
    feed_profile,
    producer,
    subscribe_request,
    redis_client,
    active_channels,
    active_poll_seconds,
    raw_topic_prefix,
):
    active_subscribed_symbols = set()
    last_active_sync = 0.0
    authenticated = False
    async with websockets.connect(alpaca_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"action": "auth", "key": alpaca_key, "secret": alpaca_secret}))

        while True:
            try:
                raw_frame = await asyncio.wait_for(ws.recv(), timeout=max(1.0, active_poll_seconds))
            except asyncio.TimeoutError:
                if authenticated:
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
                        authenticated = True
                        write_ingestor_health(
                            redis_client,
                            feed_profile,
                            status="authenticated",
                            alpacaFeed=alpaca_feed,
                            channels=list(subscribe_request.keys()),
                        )
                        print("구독 요청:", subscribe_request, flush=True)
                        await ws.send(json.dumps(subscribe_request))
                        active_subscribed_symbols = await sync_active_chart_subscriptions(
                            ws,
                            redis_client,
                            active_channels,
                            set(),
                        )
                        last_active_sync = time.monotonic()
                    continue

                if message_type == "subscription":
                    write_ingestor_health(
                        redis_client,
                        feed_profile,
                        status="subscribed",
                        alpacaFeed=alpaca_feed,
                        subscription=message,
                    )
                    print("현재 구독:", message, flush=True)
                    continue

                if message_type == "error":
                    if message.get("code") in {401, 406}:
                        authenticated = False
                    write_ingestor_health(
                        redis_client,
                        feed_profile,
                        status="error",
                        alpacaFeed=alpaca_feed,
                        alpacaError=message,
                    )
                    print("Alpaca 에러:", message, file=sys.stderr, flush=True)
                    if message.get("code") == 406 and "connection limit" in str(message.get("msg", "")).lower():
                        raise AlpacaConnectionLimitError(message.get("msg", "connection limit exceeded"))
                    continue

                if message_type in CONTROL_MESSAGE_TYPES:
                    continue

                envelope = build_raw_envelope(
                    message=message,
                    feed=alpaca_feed,
                    feed_profile=feed_profile.profile_id,
                )
                kafka_topic = raw_topic_name(raw_topic_prefix, message_type)
                kafka_key = envelope["symbol"]
                producer.send(kafka_topic, key=kafka_key, value=envelope)
                write_ingestor_health(
                    redis_client,
                    feed_profile,
                    status="ok",
                    alpacaFeed=alpaca_feed,
                    lastChannel=envelope["channel"],
                    lastSymbol=envelope["symbol"],
                    lastEventTime=envelope.get("eventTime"),
                    lastMarketSession=envelope.get("marketSession"),
                    lastSourceEventId=envelope.get("sourceEventId"),
                )
                print(f"Kafka Raw 전송: topic={kafka_topic}, key={kafka_key}, channel={envelope['channel']}", flush=True)

            if authenticated and time.monotonic() - last_active_sync >= active_poll_seconds:
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
    if not redis_url:
        return None
    validate_required_values("alpaca active subscription redis", {"redis_url": redis_url})
    return redis.from_url(redis_url, decode_responses=True)


async def sync_active_chart_subscriptions(ws, redis_client, channels, subscribed_symbols):
    if not redis_client or not channels:
        return subscribed_symbols

    desired_symbols = read_trade_subscription_symbols(redis_client)
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


def read_trade_subscription_symbols(redis_client):
    plan = resolve_trade_subscription_plan(
        active_symbols=read_active_chart_symbols(redis_client),
        watchlist_symbols=read_watchlist_symbols(redis_client),
        hot_symbols=read_hot_symbols(redis_client),
        max_symbols=os.getenv("ALPACA_MAX_TRADE_SYMBOLS"),
        max_watchlist_symbols=os.getenv("ALPACA_MAX_WATCHLIST_TRADE_SYMBOLS", "40"),
        max_hot_symbols=os.getenv("ALPACA_MAX_HOT_TRADE_SYMBOLS", "10"),
    )
    return set(plan["symbols"])


def read_active_chart_symbols(redis_client):
    keys = RedisKeyBuilder()
    symbols = set()
    for symbol in redis_client.smembers(keys.active_symbols()):
        if redis_client.exists(keys.active_symbol(symbol)):
            symbols.add(symbol)
    return symbols


def read_watchlist_symbols(redis_client):
    return read_symbol_set(redis_client, RedisKeyBuilder().watchlist_symbols())


def read_hot_symbols(redis_client):
    keys = RedisKeyBuilder()
    symbols = read_symbol_set(redis_client, keys.hot_symbols())
    snapshot_value = redis_client.get(keys.hot_symbols_snapshot())
    if not snapshot_value:
        return sorted(symbols)
    try:
        snapshot = json.loads(snapshot_value)
    except json.JSONDecodeError:
        return sorted(symbols)
    rows = snapshot.get("symbols") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return sorted(symbols)
    ordered = []
    seen = set()
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("symbol"), str):
            symbol = row["symbol"].strip().upper()
            if symbol and symbol not in seen:
                ordered.append(symbol)
                seen.add(symbol)
    for symbol in sorted(symbols):
        normalized = symbol.strip().upper()
        if normalized and normalized not in seen:
            ordered.append(normalized)
            seen.add(normalized)
    return ordered


def read_symbol_set(redis_client, key):
    try:
        return {symbol for symbol in redis_client.smembers(key) if isinstance(symbol, str)}
    except Exception:
        return set()


def parse_positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def write_ingestor_health(redis_client, feed_profile, **fields):
    if redis_client is None:
        return None
    try:
        return write_component_health(
            redis_client,
            RedisKeyBuilder(),
            f"market-ingestor-{feed_profile.profile_id}",
            feedProfile=feed_profile.profile_id,
            supportedSessions=list(feed_profile.sessions),
            **fields,
        )
    except Exception as exc:
        print(f"Ingestor health write skipped: error={exc}", file=sys.stderr, flush=True)
        return None
