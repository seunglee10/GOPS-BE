# 역할: Alpaca WebSocket에서 실시간 데이터를 받아 Kafka input topic에 저장합니다.
# 사용: ALPACA_FEED_PROFILE 또는 ALPACA_FEED를 설정하면 해당 feed runtime이 Kafka input topic에 적재합니다.
# 출력: market.input.realtime.*.v1.
import asyncio
import json
import os
import sys
import time

import redis
import websockets

from alfaka.alpaca.feed_profiles import feed_profile_active_for_session, market_session_for_now, resolve_feed_profile
from alfaka.alpaca.subscription import alpaca_subscription_symbols, build_subscription_request, load_request_config, load_symbols_and_channels, validate_channels
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.kafka_io import create_json_producer
from alfaka.common.market_messages import CONTROL_MESSAGE_TYPES, build_raw_envelope, raw_topic_name
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.runtime_health import write_component_health
from alfaka.common.runtime_config import validate_required_values
from alfaka.common.secrets import load_alpaca_credentials, resolve_alpaca_credential_source


async def main():
    load_dotenv()

    credential_source = resolve_alpaca_credential_source()
    alpaca_key, alpaca_secret = load_alpaca_credentials(credential_source)
    feed_profile = resolve_feed_profile()
    alpaca_feed = feed_profile.feed
    symbols, channels = load_symbols_and_channels()
    request_config = load_request_config()
    active_channels = parse_csv(os.getenv("ALPACA_ACTIVE_CHANNELS", ",".join(request_config.get("activeChartChannels", ["trades"]))))
    validate_channels(active_channels, request_config)
    active_channels = [channel for channel in active_channels if channel not in channels]
    active_poll_seconds = parse_positive_float(os.getenv("ALPACA_ACTIVE_POLL_SECONDS", "5"), default=5.0)
    enforce_session_window = parse_bool(os.getenv("ALPACA_ENFORCE_FEED_SESSION_WINDOW", "true"), default=True)
    session_idle_poll_seconds = parse_positive_float(os.getenv("ALPACA_SESSION_IDLE_POLL_SECONDS", "60"), default=60.0)
    raw_log_every_n = parse_positive_int(os.getenv("ALPACA_RAW_LOG_EVERY_N", "0"), default=0)

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    kafka_client_id = os.getenv("KAFKA_CLIENT_ID", "alfaka-alpaca-ingestor")
    raw_topic_prefix = os.getenv("KAFKA_INPUT_TOPIC_PREFIX", "market.input")
    validate_required_values("alpaca ingestor", {
        "kafka_servers": kafka_servers,
        "raw_topic_prefix": raw_topic_prefix,
    })

    if not alpaca_key or not alpaca_secret:
        print(
            "Alpaca credential error: category=missing_env "
            f"credentialSource={credential_source} keyId={presence(alpaca_key)} secretKey={presence(alpaca_secret)}",
            file=sys.stderr,
            flush=True,
        )
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
    print(
        "Alpaca credential: "
        f"source={credential_source} keyId={presence(alpaca_key)} secretKey={presence(alpaca_secret)} "
        f"secretName={presence(os.getenv('ALPACA_SECRET_NAME'))}",
        flush=True,
    )
    print(f"Alpaca 연결: {alpaca_url}", flush=True)
    print(f"요청 종목: count={len(symbols)} sample={symbols[:8]}", flush=True)
    print(f"요청 채널: {channels}", flush=True)
    print(f"활성 차트 tick 채널: {active_channels or 'disabled'}", flush=True)
    print(f"Kafka Input Topic Prefix: {raw_topic_prefix}", flush=True)

    delay = reconnect_backoff
    while True:
        current_session = market_session_for_now()
        if enforce_session_window and not feed_profile_active_for_session(feed_profile, current_session):
            write_ingestor_health(
                redis_client,
                feed_profile,
                status="idle",
                alpacaFeed=alpaca_feed,
                websocketUrl=alpaca_url,
                currentMarketSession=current_session,
            )
            print(
                f"Alpaca profile {feed_profile.profile_id} idle: "
                f"currentSession={current_session}, supportedSessions={','.join(feed_profile.sessions)}",
                flush=True,
            )
            await asyncio.sleep(session_idle_poll_seconds)
            continue

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
                enforce_session_window=enforce_session_window,
                raw_log_every_n=raw_log_every_n,
            )
            delay = reconnect_backoff
        except asyncio.CancelledError:
            raise
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
    enforce_session_window,
    raw_log_every_n=0,
):
    """Alpaca WebSocket 세션 하나를 열고 인증, 구독, raw Kafka 발행을 처리합니다."""
    active_subscribed_symbols = {channel: set() for channel in active_channels}
    last_active_sync = 0.0
    authenticated = False
    raw_event_count = 0
    async with websockets.connect(alpaca_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"action": "auth", "key": alpaca_key, "secret": alpaca_secret}))

        while True:
            current_session = market_session_for_now()
            if enforce_session_window and not feed_profile_active_for_session(feed_profile, current_session):
                write_ingestor_health(
                    redis_client,
                    feed_profile,
                    status="idle",
                    alpacaFeed=alpaca_feed,
                    websocketUrl=alpaca_url,
                    currentMarketSession=current_session,
                )
                print(
                    f"Alpaca profile {feed_profile.profile_id} leaving stream: "
                    f"currentSession={current_session}, supportedSessions={','.join(feed_profile.sessions)}",
                    flush=True,
                )
                return

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
                        if has_subscription_payload(subscribe_request):
                            print("구독 요청:", summarize_subscription_request(subscribe_request), flush=True)
                            await ws.send(json.dumps(subscribe_request))
                        else:
                            print("초기 구독 요청 생략: 요청 종목 없음", flush=True)
                        active_subscribed_symbols = await sync_active_chart_subscriptions(
                            ws,
                            redis_client,
                            active_channels,
                            {},
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
                    print("현재 구독:", summarize_subscription_request(message), flush=True)
                    continue

                if message_type == "error":
                    category = classify_alpaca_error(message)
                    write_ingestor_health(
                        redis_client,
                        feed_profile,
                        status="error",
                        alpacaFeed=alpaca_feed,
                        alpacaError=message,
                        errorCategory=category,
                    )
                    print(
                        "Alpaca 에러: "
                        f"category={category} code={message.get('code')} msg={message.get('msg')}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

                if message_type in CONTROL_MESSAGE_TYPES:
                    continue

                envelope = build_raw_envelope(
                    message=message,
                    feed=alpaca_feed,
                    feed_profile=feed_profile.profile_id,
                )
                attach_feed_epoch(redis_client, envelope)
                kafka_topic = raw_topic_name(raw_topic_prefix, message_type)
                kafka_key = envelope["symbol"]
                producer.send(kafka_topic, key=kafka_key, value=envelope)
                raw_event_count += 1
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
                if raw_log_every_n and raw_event_count % raw_log_every_n == 0:
                    print(
                        f"Kafka Raw 전송: count={raw_event_count}, topic={kafka_topic}, "
                        f"key={kafka_key}, channel={envelope['channel']}",
                        flush=True,
                    )

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
    enabled = os.getenv("ALPACA_ACTIVE_TICK_SUBSCRIPTION", "true").lower() not in {"0", "false", "no"}
    if not redis_url or not enabled:
        return None
    validate_required_values("alpaca active subscription redis", {"redis_url": redis_url})
    return redis.from_url(redis_url, decode_responses=True)


async def sync_active_chart_subscriptions(ws, redis_client, channels, subscribed_symbols):
    """Redis의 활성 차트 상태를 읽어 Alpaca 동적 subscribe/unsubscribe 요청을 보냅니다."""
    if not redis_client or not channels:
        return subscribed_symbols

    desired_by_channel = read_realtime_subscription_symbols_by_channel(redis_client, channels)
    next_subscribed = {}
    subscribe_request = {"action": "subscribe"}
    unsubscribe_request = {"action": "unsubscribe"}

    for channel in channels:
        current = set(subscribed_symbols.get(channel, set())) if isinstance(subscribed_symbols, dict) else set(subscribed_symbols)
        desired = desired_by_channel.get(channel, set())
        subscribe_symbols = sorted(desired - current)
        unsubscribe_symbols = sorted(current - desired)
        next_subscribed[channel] = set(desired)
        if subscribe_symbols:
            subscribe_request[channel] = alpaca_subscription_symbols(subscribe_symbols)
        if unsubscribe_symbols:
            unsubscribe_request[channel] = alpaca_subscription_symbols(unsubscribe_symbols)

    if len(subscribe_request) > 1:
        print(f"활성 차트 구독 추가: {summarize_subscription_request(subscribe_request)}", flush=True)
        await ws.send(json.dumps(subscribe_request))

    if len(unsubscribe_request) > 1:
        print(f"활성 차트 구독 해제: {summarize_subscription_request(unsubscribe_request)}", flush=True)
        await ws.send(json.dumps(unsubscribe_request))

    return next_subscribed


def has_subscription_payload(request):
    return any(key != "action" and value for key, value in request.items())


def read_realtime_subscription_symbols_by_channel(redis_client, channels):
    keys = RedisKeyBuilder()
    result = {channel: set() for channel in channels}
    for symbol in read_symbol_set(redis_client, keys.subscription_symbols()):
        layers = read_subscription_layers(redis_client, keys, symbol)
        if "trades" in layers and "trades" in result:
            result["trades"].add(symbol)
        if "quotes" in layers and "quotes" in result and "trades" in layers:
            result["quotes"].add(symbol)
        if "events" in layers and "statuses" in result:
            result["statuses"].add(symbol)
    cap = parse_positive_int(os.getenv("ALPACA_MAX_TRADE_SYMBOLS"), default=None)
    if cap:
        for channel, symbols in list(result.items()):
            result[channel] = set(sorted(symbols)[:cap])
    return result


def read_trade_subscription_symbols(redis_client):
    return read_realtime_subscription_symbols_by_channel(redis_client, ["trades"]).get("trades", set())


def read_subscription_layers(redis_client, keys, symbol):
    try:
        hgetall = getattr(redis_client, "hgetall", None)
        if callable(hgetall):
            record = hgetall(keys.subscription_symbol(symbol)) or {}
        else:
            record = getattr(redis_client, "hashes", {}).get(keys.subscription_symbol(symbol), {})
    except Exception:
        return set()
    raw_layers = record.get("layers", "")
    if isinstance(raw_layers, bytes):
        raw_layers = raw_layers.decode("utf-8")
    return {item.strip() for item in str(raw_layers).split(",") if item.strip()}


def attach_feed_epoch(redis_client, envelope):
    if not redis_client:
        return envelope
    keys = RedisKeyBuilder()
    try:
        epoch = redis_client.get(keys.feed_active_epoch())
    except Exception:
        epoch = None
    if epoch:
        envelope["feedEpoch"] = epoch.decode("utf-8") if isinstance(epoch, bytes) else str(epoch)
    return envelope


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


def parse_positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def parse_bool(value, default):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def presence(value):
    return "SET" if value else "EMPTY"


def classify_alpaca_error(message):
    code = str(message.get("code") or "")
    msg = str(message.get("msg") or "").lower()
    if code == "406" or "connection limit" in msg:
        return "connection_limit"
    if "auth" in msg and ("failed" in msg or "unauthorized" in msg or "invalid" in msg):
        return "auth_failed"
    if "auth timeout" in msg:
        return "auth_timeout"
    if "disconnected" in msg or "close" in msg:
        return "websocket_disconnected"
    return "alpaca_error"


def summarize_subscription_request(request):
    summary = {"action": request.get("action")}
    for channel, values in request.items():
        if channel == "action":
            continue
        if isinstance(values, list):
            summary[channel] = {"count": len(values), "sample": values[:8]}
        else:
            summary[channel] = values
    return summary


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
