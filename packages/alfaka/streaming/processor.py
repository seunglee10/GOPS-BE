# 역할: Kafka Raw Topic을 읽어 차트용 Processed Topic과 Redis 최신값으로 변환합니다.
# 사용: 로컬 Docker에서는 Python worker, 운영 AWS/EKS에서는 Flink Job 후보입니다.
# 출력: market.ticks.v1, market.candles.live.1m.v1, market.candles.closed.v1.
import json
import os
from collections import defaultdict

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.kafka_io import create_json_consumer, create_json_producer
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.serving.dto import market_status_event, websocket_event
from alfaka.streaming.transforms import (
    CandleAggregator,
    LiveCandleBuilder,
    MovingAverageState,
    SourceEventDeduper,
    VolumeProfileBinBuilder,
    normalize_bar,
    normalize_status,
    normalize_trade,
)


_PUBLISH_COUNTS = defaultdict(int)


def main():
    load_dotenv()
    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    raw_prefix = os.getenv("KAFKA_RAW_TOPIC_PREFIX", os.getenv("KAFKA_TOPIC_PREFIX", "market.raw"))
    group_id = os.getenv("KAFKA_FLINK_GROUP_ID", "alfaka-stream-processor")
    ticks_topic = os.getenv("KAFKA_TICKS_TOPIC", "market.ticks.v1")
    live_candle_topic = os.getenv("KAFKA_LIVE_CANDLE_TOPIC", "market.candles.live.1m.v1")
    closed_candle_topic = os.getenv("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1")
    status_topic = os.getenv("KAFKA_STATUS_TOPIC", "market.status.v1")
    profile_topic = os.getenv("KAFKA_VOLUME_PROFILE_BINS_TOPIC", "market.volume-profile-bins.1m.v1")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    log_every_n = parse_positive_int(os.getenv("PROCESSOR_LOG_EVERY_N", "500"), default=500)
    price_bin_size = parse_positive_float(os.getenv("VOLUME_PROFILE_PRICE_BIN_SIZE", "0.05"), default=0.05)

    raw_topics = [
        f"{raw_prefix}.bars",
        f"{raw_prefix}.updated-bars",
        f"{raw_prefix}.trades",
        f"{raw_prefix}.daily-bars",
        f"{raw_prefix}.statuses",
        f"{raw_prefix}.quotes",
        f"{raw_prefix}.corrections",
        f"{raw_prefix}.cancel-errors",
    ]
    consumer = create_json_consumer(raw_topics, kafka_servers, group_id, "alfaka-stream-processor")
    producer = create_json_producer(kafka_servers, "alfaka-processed-producer")
    redis_client = redis.from_url(redis_url, decode_responses=True)
    redis_keys = RedisKeyBuilder()

    live_builder = LiveCandleBuilder()
    aggregator = CandleAggregator()
    ma_state = MovingAverageState()
    deduper = SourceEventDeduper()
    profile_builder = VolumeProfileBinBuilder(price_bin_size=price_bin_size)

    print(f"Stream processor 시작: raw_topics={raw_topics}", flush=True)
    print(f"Processed Topics: {ticks_topic}, {live_candle_topic}, {closed_candle_topic}, {status_topic}, {profile_topic}", flush=True)
    print(f"Redis: {redis_url}", flush=True)

    for record in consumer:
        envelope = record.value
        channel = envelope.get("channel")
        if deduper.is_duplicate(envelope.get("sourceEventId")):
            print(f"중복 Raw event 제외: sourceEventId={envelope.get('sourceEventId')}", flush=True)
            continue

        if channel == "trades":
            trade = normalize_trade(envelope)
            live_candle = live_builder.update(trade)
            profile_bin = profile_builder.update(trade)
            publish_processed(producer, ticks_topic, trade, log_every_n)
            publish_processed(producer, live_candle_topic, live_candle, log_every_n)
            publish_processed(producer, profile_topic, profile_bin, log_every_n)
            write_trade_to_redis(redis_client, redis_keys, trade)
            write_live_candle_to_redis(redis_client, redis_keys, live_candle)
            write_volume_profile_bin_to_redis(redis_client, redis_keys, profile_bin)
            publish_chart_event(redis_client, redis_keys, websocket_event("LIVE_CANDLE_UPDATE", trade["symbol"], "1m", live_candle, feed=trade.get("feed") or "unknown"))
            continue

        if channel in {"bars", "updatedBars", "dailyBars"}:
            correction_type = "UPDATED" if channel == "updatedBars" else "NONE"
            event_type = "CANDLE_CORRECTED" if correction_type == "UPDATED" else "CANDLE_CLOSED"
            candle_1m = normalize_bar(envelope, correction_type=correction_type)
            candle_1m = ma_state.attach_ma(candle_1m)
            publish_processed(producer, closed_candle_topic, candle_1m, log_every_n)
            write_closed_candle_to_redis(redis_client, redis_keys, candle_1m)
            publish_chart_event(redis_client, redis_keys, websocket_event(event_type, candle_1m["symbol"], candle_1m["interval"], candle_1m, feed=candle_1m.get("feed") or "unknown"))

            if candle_1m["interval"] == "1m" and event_type == "CANDLE_CLOSED":
                for interval_minutes in (5, 10):
                    aggregated = aggregator.update(candle_1m, interval_minutes)
                    if aggregated:
                        aggregated = ma_state.attach_ma(aggregated)
                        publish_processed(producer, closed_candle_topic, aggregated, log_every_n)
                        write_closed_candle_to_redis(redis_client, redis_keys, aggregated)
                        publish_chart_event(redis_client, redis_keys, websocket_event("CANDLE_CLOSED", aggregated["symbol"], aggregated["interval"], aggregated, feed=aggregated.get("feed") or "unknown"))
            continue

        if channel == "statuses":
            status = normalize_status(envelope)
            publish_processed(producer, status_topic, status, log_every_n)
            write_status_to_redis(redis_client, redis_keys, status)
            publish_chart_event(redis_client, redis_keys, market_status_event(status))
            continue

        print(f"처리하지 않는 Raw channel입니다: {channel}", flush=True)


def publish_processed(producer, topic, payload, log_every_n=500):
    key = payload.get("symbol", "UNKNOWN")
    producer.send(topic, key=key, value=payload)
    # 로컬 테스트에서는 tick 수가 많아서 매 건 로그를 찍으면 처리 속도가 크게 느려집니다.
    # Kafka/Redis 전송은 계속 수행하고, 로그만 PROCESSOR_LOG_EVERY_N 건마다 줄여서 출력합니다.
    _PUBLISH_COUNTS[topic] += 1
    if _PUBLISH_COUNTS[topic] % log_every_n == 0:
        print(f"Processed Kafka 전송: topic={topic}, key={key}, count={_PUBLISH_COUNTS[topic]}", flush=True)


def write_trade_to_redis(redis_client, redis_keys, trade):
    key = redis_keys.price_latest(trade["symbol"])
    redis_client.hset(key, mapping={"symbol": trade["symbol"], "price": trade["price"], "size": trade.get("size") or 0, "timestamp": trade["timestamp"], "source": "alpaca.trades"})
    redis_client.expire(key, 86400)


def write_live_candle_to_redis(redis_client, redis_keys, candle):
    key = redis_keys.live_candle(candle["symbol"])
    redis_client.set(key, json.dumps(candle, ensure_ascii=False, separators=(",", ":")))
    redis_client.expire(key, 86400)


def write_closed_candle_to_redis(redis_client, redis_keys, candle):
    latest_key = redis_keys.latest_candle(candle["symbol"], candle["interval"])
    series_key = redis_keys.recent_candles(candle["symbol"], candle["interval"])
    candle_json = json.dumps(candle, ensure_ascii=False, separators=(",", ":"))
    score = timestamp_score(candle["timestamp"])
    redis_client.set(latest_key, candle_json)
    redis_client.zremrangebyscore(series_key, score, score)
    redis_client.zadd(series_key, {candle_json: score})
    redis_client.expire(latest_key, 86400)
    redis_client.expire(series_key, 604800)


def write_status_to_redis(redis_client, redis_keys, status):
    status_json = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
    redis_client.set(redis_keys.market_status_latest(), status_json)
    redis_client.expire(redis_keys.market_status_latest(), 86400)
    symbol = status.get("symbol")
    if symbol and symbol != "_MARKET":
        key = redis_keys.market_status_symbol_latest(symbol)
        redis_client.set(key, status_json)
        redis_client.expire(key, 86400)


def write_volume_profile_bin_to_redis(redis_client, redis_keys, profile_bin):
    key = redis_keys.volume_profile_live(profile_bin["symbol"])
    member = json.dumps(profile_bin, ensure_ascii=False, separators=(",", ":"))
    score = timestamp_score(profile_bin["eventMinute"])
    redis_client.zadd(key, {member: score})
    redis_client.expire(key, 86400)


def publish_chart_event(redis_client, redis_keys, event):
    event_json = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    redis_client.publish(redis_keys.market_events_symbol(event["symbol"]), event_json)
    redis_client.publish(redis_keys.market_events(), event_json)


def timestamp_score(timestamp):
    from datetime import datetime
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def parse_positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def parse_positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
