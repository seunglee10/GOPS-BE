# 역할: Kafka Raw Topic을 읽어 차트용 Processed Topic과 Redis 최신값으로 변환합니다.
# 사용: 로컬 Docker와 현재 AWS 배포에서는 Python market-processor runtime으로 실행합니다.
# 출력: market.ticks.v1, market.candles.live.1m.v1, market.candles.closed.v1.
import json
import os
from collections import defaultdict

import redis

from alfaka.alpaca.subscription import configured_collection_symbols
from alfaka.common.env import load_dotenv
from alfaka.common.env import parse_csv
from alfaka.common.kafka_io import create_json_consumer, create_json_producer
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.runtime_health import write_component_health
from alfaka.common.runtime_config import validate_required_values
from alfaka.serving.dto import market_status_event, websocket_event
from alfaka.serving.intervals import redis_closed_candle_cap
from alfaka.streaming.transforms import (
    CandleAggregator,
    LiveCandleBuilder,
    MovingAverageState,
    ProvisionalCandleState,
    SourceEventDeduper,
    VolumeProfileBinBuilder,
    normalize_bar,
    normalize_status,
    normalize_trade,
)


_PUBLISH_COUNTS = defaultdict(int)


def main():
    load_dotenv()
    config = processor_runtime_config()
    kafka_servers = config["kafka_servers"]
    group_id = config["group_id"]
    ticks_topic = config["ticks_topic"]
    live_candle_topic = config["live_candle_topic"]
    closed_candle_topic = config["closed_candle_topic"]
    status_topic = config["status_topic"]
    profile_topic = config["profile_topic"]
    redis_url = config["redis_url"]
    log_every_n = config["log_every_n"]
    price_bin_size = config["price_bin_size"]
    raw_topics = config["raw_topics"]

    consumer = create_json_consumer(raw_topics, kafka_servers, group_id, "alfaka-stream-processor")
    producer = create_json_producer(kafka_servers, "alfaka-processed-producer")
    redis_client = redis.from_url(redis_url, decode_responses=True)
    redis_keys = RedisKeyBuilder()

    state = ProcessorState(price_bin_size=price_bin_size)
    recover_processor_state_from_redis(redis_client, redis_keys, state, config["recovery_symbols"])
    if config["clickhouse_recovery_enabled"]:
        recover_processor_state_from_clickhouse(state, config["recovery_symbols"])
    topics = {
        "ticks": ticks_topic,
        "live_candle": live_candle_topic,
        "closed_candle": closed_candle_topic,
        "status": status_topic,
        "profile": profile_topic,
    }

    print(f"Stream processor 시작: raw_topics={raw_topics}", flush=True)
    print(f"Processed Topics: {ticks_topic}, {live_candle_topic}, {closed_candle_topic}, {status_topic}, {profile_topic}", flush=True)
    print(f"Redis: {redis_url}", flush=True)

    for record in consumer:
        process_raw_envelope(record.value, producer, redis_client, redis_keys, state, topics, log_every_n=log_every_n)


def processor_runtime_config(environ=None):
    environ = environ or os.environ
    kafka_servers = environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    raw_prefix = environ.get("KAFKA_RAW_TOPIC_PREFIX", environ.get("KAFKA_TOPIC_PREFIX", "market.raw"))
    group_id = environ.get("KAFKA_PROCESSOR_GROUP_ID") or environ.get("KAFKA_FLINK_GROUP_ID") or "alfaka-stream-processor"
    config = {
        "kafka_servers": kafka_servers,
        "raw_prefix": raw_prefix,
        "group_id": group_id,
        "ticks_topic": environ.get("KAFKA_TICKS_TOPIC", "market.ticks.v1"),
        "live_candle_topic": environ.get("KAFKA_LIVE_CANDLE_TOPIC", "market.candles.live.1m.v1"),
        "closed_candle_topic": environ.get("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1"),
        "status_topic": environ.get("KAFKA_STATUS_TOPIC", "market.status.v1"),
        "profile_topic": environ.get("KAFKA_VOLUME_PROFILE_BINS_TOPIC", "market.volume-profile-bins.1m.v1"),
        "redis_url": environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "log_every_n": parse_positive_int(environ.get("PROCESSOR_LOG_EVERY_N", "500"), default=500),
        "price_bin_size": parse_positive_float(environ.get("VOLUME_PROFILE_PRICE_BIN_SIZE", "0.05"), default=0.05),
        "recovery_symbols": processor_recovery_symbols(environ),
        "clickhouse_recovery_enabled": parse_bool(environ.get("PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED", "false")),
    }
    config["raw_topics"] = [
        f"{raw_prefix}.bars",
        f"{raw_prefix}.updated-bars",
        f"{raw_prefix}.trades",
        f"{raw_prefix}.daily-bars",
        f"{raw_prefix}.statuses",
        f"{raw_prefix}.quotes",
        f"{raw_prefix}.corrections",
        f"{raw_prefix}.cancel-errors",
    ]
    validate_processor_runtime_config(config)
    return config


def validate_processor_runtime_config(config):
    validate_required_values("market processor", {
        "kafka_servers": config.get("kafka_servers"),
        "raw_prefix": config.get("raw_prefix"),
        "group_id": config.get("group_id"),
        "ticks_topic": config.get("ticks_topic"),
        "live_candle_topic": config.get("live_candle_topic"),
        "closed_candle_topic": config.get("closed_candle_topic"),
        "status_topic": config.get("status_topic"),
        "profile_topic": config.get("profile_topic"),
        "redis_url": config.get("redis_url"),
    })


class ProcessorState:
    def __init__(self, price_bin_size=0.05):
        self.live_builder = LiveCandleBuilder()
        self.provisional_state = ProvisionalCandleState()
        self.aggregator = CandleAggregator()
        self.ma_state = MovingAverageState()
        self.deduper = SourceEventDeduper()
        self.profile_builder = VolumeProfileBinBuilder(price_bin_size=price_bin_size)


def processor_recovery_symbols(environ=None):
    environ = environ or os.environ
    explicit = parse_csv(environ.get("PROCESSOR_RECOVERY_SYMBOLS", ""))
    if explicit:
        return [symbol.upper() for symbol in explicit]
    if environ is not os.environ:
        return []
    try:
        return configured_collection_symbols()
    except Exception as exc:
        print(f"Processor Redis recovery symbol resolution skipped: {exc}", flush=True)
        return []


def recover_processor_state_from_redis(redis_client, redis_keys, state, symbols):
    if not symbols:
        return {"symbols": 0, "closed": {"1m": 0, "1D": 0}, "live1m": 0}

    recovered = {"symbols": 0, "closed": {"1m": 0, "1D": 0}, "live1m": 0}
    for symbol in symbols:
        symbol_recovered = False
        for interval in ("1m", "1D"):
            candles = read_recent_closed_candles_from_redis(redis_client, redis_keys, symbol, interval)
            for candle in candles:
                state.provisional_state.record_closed(candle)
            if candles:
                recovered["closed"][interval] += len(candles)
                symbol_recovered = True
        live_candle = read_live_candle_from_redis(redis_client, redis_keys, symbol, "1m")
        if live_candle and state.live_builder.seed(live_candle):
            recovered["live1m"] += 1
            symbol_recovered = True
        if symbol_recovered:
            recovered["symbols"] += 1

    print(
        "Processor Redis recovery: "
        f"symbols={recovered['symbols']} "
        f"closed1m={recovered['closed']['1m']} "
        f"closed1D={recovered['closed']['1D']} "
        f"live1m={recovered['live1m']}",
        flush=True,
    )
    return recovered


def recover_processor_state_from_clickhouse(state, symbols, provider=None):
    if not symbols:
        return {"symbols": 0, "closed": {"1m": 0, "1D": 0}}

    if provider is None:
        from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
        provider = ClickHouseMarketDataProvider()

    recovered = {"symbols": 0, "closed": {"1m": 0, "1D": 0}}
    for symbol in symbols:
        symbol_recovered = False
        for interval in ("1m", "1D"):
            try:
                candles = provider.candles(symbol, interval, redis_closed_candle_cap(interval))
            except Exception as exc:
                print(f"Processor ClickHouse recovery skipped: symbol={symbol} interval={interval} error={exc}", flush=True)
                continue
            for candle in candles:
                normalized = {
                    "eventType": "CANDLE",
                    **candle,
                    "symbol": candle.get("symbol") or symbol,
                    "interval": interval,
                    "isClosed": bool(candle.get("isClosed", candle.get("is_closed", True))),
                }
                state.provisional_state.record_closed(normalized)
            if candles:
                recovered["closed"][interval] += len(candles)
                symbol_recovered = True
        if symbol_recovered:
            recovered["symbols"] += 1

    print(
        "Processor ClickHouse recovery: "
        f"symbols={recovered['symbols']} "
        f"closed1m={recovered['closed']['1m']} "
        f"closed1D={recovered['closed']['1D']}",
        flush=True,
    )
    return recovered


def read_recent_closed_candles_from_redis(redis_client, redis_keys, symbol, interval):
    key = redis_keys.recent_candles(symbol, interval)
    try:
        rows = redis_client.zrange(key, 0, -1)
    except Exception as exc:
        print(f"Processor Redis recovery skipped closed candles: symbol={symbol} interval={interval} error={exc}", flush=True)
        return []
    return [json.loads(row) for row in rows]


def read_live_candle_from_redis(redis_client, redis_keys, symbol, interval):
    try:
        value = redis_client.get(redis_keys.live_candle(symbol, interval))
    except Exception as exc:
        print(f"Processor Redis recovery skipped live candle: symbol={symbol} interval={interval} error={exc}", flush=True)
        return None
    return json.loads(value) if value else None


def process_raw_envelope(envelope, producer, redis_client, redis_keys, state, topics, log_every_n=500):
    channel = envelope.get("channel")
    if state.deduper.is_duplicate(envelope.get("sourceEventId")):
        print(f"중복 Raw event 제외: sourceEventId={envelope.get('sourceEventId')}", flush=True)
        write_processor_health(redis_client, redis_keys, envelope, result="duplicate")
        return "duplicate"

    if channel == "trades":
        trade = normalize_trade(envelope)
        live_candle = state.live_builder.update(trade)
        profile_bin = state.profile_builder.update(trade)
        publish_processed(producer, topics["ticks"], trade, log_every_n)
        publish_processed(producer, topics["live_candle"], live_candle, log_every_n)
        publish_processed(producer, topics["profile"], profile_bin, log_every_n)
        write_trade_to_redis(redis_client, redis_keys, trade)
        publish_live_candle(redis_client, redis_keys, live_candle, feed=trade.get("feed") or "unknown")
        write_volume_profile_bin_to_redis(redis_client, redis_keys, profile_bin)
        publish_derived_live_candles(redis_client, redis_keys, state, trade["symbol"], live_1m=live_candle)
        write_processor_health(redis_client, redis_keys, envelope, result="trades")
        return "trades"

    if channel in {"bars", "updatedBars", "dailyBars"}:
        correction_type = "UPDATED" if channel == "updatedBars" else "NONE"
        event_type = "CANDLE_CORRECTED" if correction_type == "UPDATED" else "CANDLE_CLOSED"
        candle_1m = normalize_bar(envelope, correction_type=correction_type)
        candle_1m = state.ma_state.attach_ma(candle_1m)
        publish_processed(producer, topics["closed_candle"], candle_1m, log_every_n)
        write_closed_candle_to_redis(redis_client, redis_keys, candle_1m)
        publish_chart_event(redis_client, redis_keys, websocket_event(event_type, candle_1m["symbol"], candle_1m["interval"], candle_1m, feed=candle_1m.get("feed") or "unknown"))
        state.provisional_state.record_closed(candle_1m)

        if candle_1m["interval"] == "1m":
            if event_type == "CANDLE_CLOSED":
                for interval_minutes in (5, 10):
                    aggregated = state.aggregator.update(candle_1m, interval_minutes)
                    if aggregated:
                        aggregated = state.ma_state.attach_ma(aggregated)
                        publish_processed(producer, topics["closed_candle"], aggregated, log_every_n)
                        write_closed_candle_to_redis(redis_client, redis_keys, aggregated)
                        publish_chart_event(redis_client, redis_keys, websocket_event("CANDLE_CLOSED", aggregated["symbol"], aggregated["interval"], aggregated, feed=aggregated.get("feed") or "unknown"))
            publish_derived_live_candles(redis_client, redis_keys, state, candle_1m["symbol"], anchor_1m_timestamp=candle_1m["timestamp"])
        elif candle_1m["interval"] == "1D":
            publish_daily_derived_live_candles(redis_client, redis_keys, state, candle_1m["symbol"], anchor_1d_timestamp=candle_1m["timestamp"])
        write_processor_health(redis_client, redis_keys, envelope, result=channel)
        return channel

    if channel == "statuses":
        status = normalize_status(envelope)
        publish_processed(producer, topics["status"], status, log_every_n)
        write_status_to_redis(redis_client, redis_keys, status)
        publish_chart_event(redis_client, redis_keys, market_status_event(status))
        write_processor_health(redis_client, redis_keys, envelope, result="statuses")
        return "statuses"

    print(f"처리하지 않는 Raw channel입니다: {channel}", flush=True)
    write_processor_health(redis_client, redis_keys, envelope, result="ignored")
    return "ignored"


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
    redis_client.hset(key, mapping={
        "symbol": trade["symbol"],
        "price": trade["price"],
        "size": trade.get("size") or 0,
        "timestamp": trade["timestamp"],
        "source": "alpaca.trades",
        "feed": trade.get("feed") or "unknown",
        "feedProfile": trade.get("feedProfile") or trade.get("feed") or "unknown",
        "marketSession": trade.get("marketSession") or "unknown",
    })
    redis_client.expire(key, 86400)


def write_live_candle_to_redis(redis_client, redis_keys, candle):
    key = redis_keys.live_candle(candle["symbol"], candle.get("interval", "1m"))
    redis_client.set(key, json.dumps(candle, ensure_ascii=False, separators=(",", ":")))
    redis_client.expire(key, 86400)


def publish_live_candle(redis_client, redis_keys, candle, feed="unknown"):
    write_live_candle_to_redis(redis_client, redis_keys, candle)
    publish_chart_event(
        redis_client,
        redis_keys,
        websocket_event(
            "LIVE_CANDLE_UPDATE",
            candle["symbol"],
            candle["interval"],
            candle,
            source=candle.get("source") or "derived.live",
            feed=feed or candle.get("feed") or "unknown",
        ),
    )


def publish_derived_live_candles(redis_client, redis_keys, state, symbol, live_1m=None, anchor_1m_timestamp=None):
    provisional_1d = None
    for interval in ("5m", "10m", "1D"):
        candle = state.provisional_state.build_from_1m(
            symbol,
            interval,
            anchor_timestamp=anchor_1m_timestamp,
            live_1m=live_1m,
        )
        if not candle:
            continue
        publish_live_candle(redis_client, redis_keys, candle, feed=candle.get("feed") or "unknown")
        if interval == "1D":
            provisional_1d = candle
    if provisional_1d:
        publish_daily_derived_live_candles(redis_client, redis_keys, state, symbol, provisional_1d=provisional_1d)


def publish_daily_derived_live_candles(redis_client, redis_keys, state, symbol, provisional_1d=None, anchor_1d_timestamp=None):
    for interval in ("1W", "1M"):
        candle = state.provisional_state.build_from_1d(
            symbol,
            interval,
            anchor_timestamp=anchor_1d_timestamp,
            provisional_1d=provisional_1d,
        )
        if candle:
            publish_live_candle(redis_client, redis_keys, candle, feed=candle.get("feed") or "unknown")


def write_closed_candle_to_redis(redis_client, redis_keys, candle):
    latest_key = redis_keys.latest_candle(candle["symbol"], candle["interval"])
    series_key = redis_keys.recent_candles(candle["symbol"], candle["interval"])
    candle_json = json.dumps(candle, ensure_ascii=False, separators=(",", ":"))
    score = timestamp_score(candle["timestamp"])
    redis_client.set(latest_key, candle_json)
    redis_client.zremrangebyscore(series_key, score, score)
    redis_client.zadd(series_key, {candle_json: score})
    cap = redis_closed_candle_cap(candle["interval"])
    redis_client.zremrangebyrank(series_key, 0, -cap - 1)
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


def write_processor_health(redis_client, redis_keys, envelope, result):
    try:
        write_component_health(
            redis_client,
            redis_keys,
            "market-processor",
            status="ok",
            lastResult=result,
            lastChannel=envelope.get("channel"),
            lastSymbol=envelope.get("symbol") or (envelope.get("raw") or {}).get("S"),
            lastEventTime=envelope.get("eventTime") or (envelope.get("raw") or {}).get("t"),
            lastFeed=envelope.get("feed"),
            lastFeedProfile=envelope.get("feedProfile"),
            lastMarketSession=envelope.get("marketSession"),
            lastSourceEventId=envelope.get("sourceEventId"),
        )
    except Exception as exc:
        print(f"Processor health heartbeat write skipped: error={exc}", flush=True)


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


def parse_bool(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
