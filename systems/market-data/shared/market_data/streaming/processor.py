# 역할: Kafka Raw Topic을 읽어 차트용 Processed Topic과 Redis 최신값으로 변환합니다.
# 사용: 로컬 Docker와 현재 AWS 배포에서는 Python market-processor runtime으로 실행합니다.
# 출력: docs/CHART_DATA_ARCHITECTURE.md의 layer topic과 Redis live/cache state.
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import redis

from market_data.alpaca.subscription import configured_collection_symbols
from market_data.common.env import load_dotenv
from market_data.common.env import parse_csv
from market_data.common.kafka_io import create_json_consumer, create_json_producer
from market_data.common.kafka_topics import closed_candle_topics_from_env, default_closed_candle_topics
from market_data.common.heartbeat import touch_heartbeat
from market_data.common.redis_keys import RedisKeyBuilder
from market_data.common.runtime_health import write_component_health
from market_data.common.runtime_config import validate_required_values
from market_data.orderflow import (
    OrderFlowBinBuilder,
    PinnedQuoteCache,
    classify_trade_side,
    health_write_min_interval_ms_from_env,
    live_minute_ttl_seconds_from_env,
    live_ttl_seconds_from_env,
    pinned_symbols_from_env,
    price_bin_size_from_env,
    publish_throttle_ms_from_env,
    quote_event_publish_min_interval_ms_from_env,
    quote_future_tolerance_ms_from_env,
    quote_max_age_ms_from_env,
    quote_refresh_ms_from_env,
    quote_redis_write_min_interval_ms_from_env,
    redis_flush_ms_from_env,
    trade_redis_write_min_interval_ms_from_env,
)
from market_data.orderflow.redis_model import (
    encode_order_flow_minute_blob,
    order_flow_blob_to_bins,
    order_flow_minute_blob,
    order_flow_minute_score,
    parse_order_flow_minute_blob,
)
from market_data.serving.dto import market_status_event, order_flow_event, websocket_event
from market_data.serving.closed_watermark import (
    candle_at_or_before_watermark,
    candle_watermark_value,
    latest_watermark_value,
    live_candle_at_or_before_watermark,
    watermark_after,
)
from market_data.serving.intervals import redis_closed_candle_cap
from market_data.serving.session_buckets import BUCKET_POLICY_REGULAR_SESSION
from market_data.streaming.transforms import (
    CalendarCandleAggregator,
    CandleAggregator,
    LiveCandleBuilder,
    MovingAverageState,
    ProvisionalCandleState,
    SourceEventDeduper,
    TickWindowCandleBuilder,
    normalize_bar,
    normalize_candle_numeric_fields,
    normalize_quote,
    normalize_status,
    normalize_trade,
    floor_minute,
    parse_time,
    to_iso,
)


_PUBLISH_COUNTS = defaultdict(int)
ORDER_FLOW_MARKET_TIMEZONE = ZoneInfo("America/New_York")


def main():
    load_dotenv()
    touch_heartbeat()
    config = processor_runtime_config()
    kafka_servers = config["kafka_servers"]
    group_id = config["group_id"]
    status_topic = config["status_topic"]
    redis_url = config["redis_url"]
    log_every_n = config["log_every_n"]
    raw_topics = config["raw_topics"]

    consumer = create_json_consumer(
        raw_topics,
        kafka_servers,
        group_id,
        "alfaka-stream-processor",
        enable_auto_commit=config["enable_auto_commit"],
        partition_assignment_strategy="range",
    )
    producer = create_json_producer(kafka_servers, "alfaka-processed-producer")
    redis_client = redis.from_url(redis_url, decode_responses=True)
    redis_keys = RedisKeyBuilder()

    state = ProcessorState(
        watermark_grace_seconds=config["watermark_grace_seconds"],
        live_publish_min_interval_seconds=config["live_publish_min_interval_seconds"],
        active_feed_cache_seconds=config["active_feed_cache_seconds"],
        order_flow_quote_cache_only=config["order_flow_quote_cache_only"],
    )
    state.canonical_candle_loader = clickhouse_canonical_candle_loader()
    configure_order_flow_state(state, redis_client, redis_keys)
    recover_processor_state_from_redis(redis_client, redis_keys, state, config["recovery_symbols"])
    if config["clickhouse_recovery_enabled"]:
        recover_processor_state_from_clickhouse(state, config["recovery_symbols"])
    topics = {
        "trades": config["trades_topic"],
        "quotes": config["quotes_topic"],
        "tick_fanout": config["tick_fanout_topics"],
        "closed_candles": config["closed_candle_topic"],
        "status": status_topic,
        "events": config["events_topic"],
        "tick_fanout_enabled": config["publish_tick_fanout"],
    }

    print(f"Stream processor 시작: raw_topics={raw_topics}", flush=True)
    print(f"Processed Topics: {topics}", flush=True)
    print(f"Redis: {redis_url}", flush=True)

    run_stream_processor(
        consumer,
        producer,
        redis_client,
        redis_keys,
        state,
        topics,
        log_every_n=log_every_n,
        poll_timeout_ms=config["poll_timeout_ms"],
        flush_interval_seconds=config["flush_interval_seconds"],
        enable_auto_commit=config["enable_auto_commit"],
    )


def processor_runtime_config(environ=None):
    environ = environ or os.environ
    kafka_servers = environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = environ.get("KAFKA_PROCESSOR_GROUP_ID") or "alfaka-market-processor"
    input_prefix = environ.get("KAFKA_INPUT_TOPIC_PREFIX", "market.input")
    realtime_prefix = environ.get("KAFKA_REALTIME_TICK_TOPIC_PREFIX", "market.realtime.ticks.to")
    raw_topic_override = parse_csv(environ.get("KAFKA_PROCESSOR_RAW_TOPICS", ""))
    tick_fanout_topics = {
        "1m": environ.get("KAFKA_REALTIME_TICKS_TO_1M_TOPIC", f"{realtime_prefix}.1m.v1"),
        "5m": environ.get("KAFKA_REALTIME_TICKS_TO_5M_TOPIC", f"{realtime_prefix}.5m.v1"),
        "10m": environ.get("KAFKA_REALTIME_TICKS_TO_10M_TOPIC", f"{realtime_prefix}.10m.v1"),
        "1D": environ.get("KAFKA_REALTIME_TICKS_TO_1D_TOPIC", f"{realtime_prefix}.1d.v1"),
        "1W": environ.get("KAFKA_REALTIME_TICKS_TO_1W_TOPIC", f"{realtime_prefix}.1w.v1"),
        "1M": environ.get("KAFKA_REALTIME_TICKS_TO_1MO_TOPIC", f"{realtime_prefix}.1mo.v1"),
    }
    enabled_tick_fanout_intervals = [] if raw_topic_override else parse_tick_fanout_intervals(
        environ.get("KAFKA_TICK_FANOUT_INTERVALS", ""),
        tick_fanout_topics,
    )
    config = {
        "kafka_servers": kafka_servers,
        "input_prefix": input_prefix,
        "group_id": group_id,
        "trades_topic": environ.get("KAFKA_TRADES_LAYER_TOPIC", "market.layer.trades.v1"),
        "quotes_topic": environ.get("KAFKA_QUOTES_LAYER_TOPIC", "market.layer.quotes.v1"),
        "events_topic": environ.get("KAFKA_EVENTS_LAYER_TOPIC", "market.layer.events.v1"),
        "status_topic": environ.get("KAFKA_STATUS_TOPIC", "market.layer.events.v1"),
        "tick_fanout_topics": {interval: tick_fanout_topics[interval] for interval in enabled_tick_fanout_intervals},
        "closed_candle_topic": closed_candle_topics_from_env(environ),
        "redis_url": environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "log_every_n": parse_positive_int(environ.get("PROCESSOR_LOG_EVERY_N", "500"), default=500),
        "watermark_grace_seconds": parse_positive_float(environ.get("CANDLE_WATERMARK_GRACE_SECONDS", "5"), default=5),
        "flush_interval_seconds": parse_positive_float(environ.get("CANDLE_FLUSH_INTERVAL_SECONDS", "1"), default=1),
        "live_publish_min_interval_seconds": parse_non_negative_float(environ.get("LIVE_CANDLE_PUBLISH_MIN_INTERVAL_SECONDS", "0"), default=0),
        "active_feed_cache_seconds": parse_non_negative_float(environ.get("PROCESSOR_ACTIVE_FEED_CACHE_SECONDS", "1"), default=1),
        "order_flow_quote_cache_only": parse_bool(environ.get("ORDER_FLOW_QUOTE_CACHE_ONLY", "false")),
        "poll_timeout_ms": parse_positive_int(environ.get("PROCESSOR_POLL_TIMEOUT_MS", "1000"), default=1000),
        "enable_auto_commit": parse_bool(environ.get("KAFKA_PROCESSOR_ENABLE_AUTO_COMMIT", "false")),
        "publish_tick_fanout": parse_bool(environ.get("KAFKA_PUBLISH_TICK_FANOUT", "false")),
        "recovery_symbols": processor_recovery_symbols(environ),
        "clickhouse_recovery_enabled": parse_bool(environ.get("PROCESSOR_RECOVERY_CLICKHOUSE_ENABLED", "false")),
    }
    if raw_topic_override:
        config["raw_topics"] = raw_topic_override
    else:
        config["raw_topics"] = [
            environ.get("KAFKA_INPUT_TRADES_TOPIC", f"{input_prefix}.realtime.trades.v1"),
            environ.get("KAFKA_INPUT_QUOTES_TOPIC", f"{input_prefix}.realtime.quotes.v1"),
            environ.get("KAFKA_INPUT_BARS_1M_TOPIC", f"{input_prefix}.realtime.bars.1m.v1"),
            environ.get("KAFKA_INPUT_UPDATED_BARS_1M_TOPIC", f"{input_prefix}.realtime.updated-bars.1m.v1"),
            environ.get("KAFKA_INPUT_DAILY_BARS_TOPIC", f"{input_prefix}.realtime.daily-bars.v1"),
            environ.get("KAFKA_INPUT_EVENTS_TOPIC", f"{input_prefix}.realtime.events.v1"),
            *config["tick_fanout_topics"].values(),
        ]
    validate_processor_runtime_config(config)
    return config


def parse_tick_fanout_intervals(value, available_topics):
    requested = [normalize_tick_fanout_interval(item) for item in parse_csv(value or "")]
    if not requested:
        return []
    if requested == ["all"]:
        return list(available_topics)
    return [interval for interval in requested if interval in available_topics]


def normalize_tick_fanout_interval(value):
    normalized = str(value or "").strip()
    aliases = {
        "1min": "1m",
        "5min": "5m",
        "10min": "10m",
        "1d": "1D",
        "1day": "1D",
        "1w": "1W",
        "1week": "1W",
        "1mo": "1M",
        "1month": "1M",
    }
    return aliases.get(normalized.lower(), normalized)


def validate_processor_runtime_config(config):
    validate_required_values("market processor", {
        "kafka_servers": config.get("kafka_servers"),
        "input_prefix": config.get("input_prefix"),
        "group_id": config.get("group_id"),
        "trades_topic": config.get("trades_topic"),
        "quotes_topic": config.get("quotes_topic"),
        "events_topic": config.get("events_topic"),
        "status_topic": config.get("status_topic"),
        "closed_candle_topic": config.get("closed_candle_topic"),
        "raw_topics": config.get("raw_topics"),
        "redis_url": config.get("redis_url"),
    })


class ProcessorState:
    def __init__(
        self,
        watermark_grace_seconds=5,
        live_publish_min_interval_seconds=0,
        active_feed_cache_seconds=1,
        order_flow_quote_cache_only=False,
    ):
        self.live_builder = LiveCandleBuilder()
        self.window_builder = TickWindowCandleBuilder(grace_seconds=watermark_grace_seconds)
        self.provisional_state = ProvisionalCandleState()
        self.aggregator = CandleAggregator(bucket_policy=BUCKET_POLICY_REGULAR_SESSION)
        self.daily_aggregator = CalendarCandleAggregator("1m", "1D")
        self.weekly_aggregator = CalendarCandleAggregator("1D", "1W")
        self.monthly_aggregator = CalendarCandleAggregator("1D", "1M")
        self.ma_state = MovingAverageState()
        self.deduper = SourceEventDeduper()
        self.live_publish_throttle = LiveCandlePublishThrottle(live_publish_min_interval_seconds)
        self.active_feed_cache = ActiveFeedCache(active_feed_cache_seconds)
        self.order_flow_builder = None
        self.order_flow_quote_cache = None
        self.order_flow_quote_cache_only = bool(order_flow_quote_cache_only)
        self.order_flow_quote_max_age_ms = None
        self.order_flow_quote_future_tolerance_ms = 0
        self.order_flow_publish_state = {}
        self.order_flow_redis_flush_state = {}
        self.order_flow_minutes_ttl_state = set()
        self.quote_redis_write_state = {}
        self.quote_event_publish_state = {}
        self.trade_redis_write_state = {}
        self.health_write_state = {}
        self.canonical_candle_loader = None
        self.correction_recompute_failures = 0


def configure_order_flow_state(state, redis_client, redis_keys):
    pinned_symbols = pinned_symbols_from_env()
    if not pinned_symbols:
        return state
    state.order_flow_builder = OrderFlowBinBuilder(
        price_bin_size=price_bin_size_from_env(),
        pinned_symbols=pinned_symbols,
    )
    state.order_flow_quote_cache = PinnedQuoteCache(
        redis_client,
        redis_keys,
        refresh_ms=quote_refresh_ms_from_env(),
        redis_fallback=not getattr(state, "order_flow_quote_cache_only", False),
        pinned_symbols=pinned_symbols,
    )
    state.order_flow_quote_max_age_ms = quote_max_age_ms_from_env()
    state.order_flow_quote_future_tolerance_ms = quote_future_tolerance_ms_from_env()
    state.order_flow_publish_state = {}
    state.order_flow_redis_flush_state = {}
    state.order_flow_minutes_ttl_state = set()
    restore_order_flow_live_minutes(state, redis_client, redis_keys)
    return state


def restore_order_flow_live_minutes(state, redis_client, redis_keys):
    builder = getattr(state, "order_flow_builder", None)
    if builder is None:
        return 0
    flush_state = _order_flow_redis_flush_state(state)
    restored = 0
    for symbol in sorted(builder.pinned_symbols):
        try:
            blob = parse_order_flow_minute_blob(redis_client.get(redis_keys.order_flow_live_minute(symbol)))
        except Exception as exc:
            print(f"Order-flow live-minute restore skipped: symbol={symbol} error={exc}", flush=True)
            continue
        if not blob or str(blob.get("symbol") or "").upper() != symbol:
            continue
        event_minute = str(blob.get("eventMinute") or "")
        bins = order_flow_blob_to_bins(blob)
        restored_count = builder.restore_minute(symbol, event_minute, bins)
        if restored_count <= 0:
            continue
        flush_state[symbol] = {"minute": event_minute, "lastFlush": 0.0}
        restored += 1
    return restored


class LiveCandlePublishThrottle:
    def __init__(self, min_interval_seconds=0):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds or 0))
        self.last_published = {}

    def should_publish(self, candle):
        if self.min_interval_seconds <= 0:
            return True
        key = (candle.get("symbol"), candle.get("interval"))
        bucket = candle.get("timestamp")
        event_time = candle.get("updatedAt") or candle.get("timestamp")
        try:
            event_score = parse_time(event_time).timestamp()
        except Exception:
            event_score = datetime.now(timezone.utc).timestamp()
        last = self.last_published.get(key)
        if not last or last["bucket"] != bucket or event_score - last["event_score"] >= self.min_interval_seconds:
            self.last_published[key] = {"bucket": bucket, "event_score": event_score}
            return True
        return False


class ActiveFeedCache:
    def __init__(self, ttl_seconds=1):
        self.ttl_seconds = max(0.0, float(ttl_seconds or 0))
        self.loaded_at = None
        self.value = None

    def get(self, loader):
        if self.ttl_seconds <= 0:
            return loader()
        now = datetime.now(timezone.utc).timestamp()
        if self.loaded_at is None or now - self.loaded_at >= self.ttl_seconds:
            self.value = loader()
            self.loaded_at = now
        return self.value


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
        from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider
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


def run_stream_processor(
    consumer,
    producer,
    redis_client,
    redis_keys,
    state,
    topics,
    log_every_n=500,
    poll_timeout_ms=1000,
    flush_interval_seconds=1,
    enable_auto_commit=False,
    now_fn=None,
):
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    last_flush_at = 0.0
    failed = False
    try:
        while True:
            batches = consumer.poll(timeout_ms=poll_timeout_ms)
            touch_heartbeat()
            had_records = False
            for records in batches.values():
                for record in records:
                    had_records = True
                    process_raw_envelope(record.value, producer, redis_client, redis_keys, state, topics, log_every_n=log_every_n)

            now = now_fn()
            if had_records:
                producer.flush()

            if had_records or now.timestamp() - last_flush_at >= flush_interval_seconds:
                reference_time = flush_reference_time(state, now=now, allow_wall_clock=not had_records)
                published = flush_ready_closed_candles(
                    producer,
                    redis_client,
                    redis_keys,
                    state,
                    topics,
                    reference_time=reference_time,
                    log_every_n=log_every_n,
                )
                if published:
                    producer.flush()
                last_flush_at = now.timestamp()

            if had_records and not enable_auto_commit:
                commit_consumer(consumer)
    except KeyboardInterrupt:
        print("Stream processor 종료 신호 수신: ready window를 flush합니다.", flush=True)
    except Exception:
        failed = True
        raise
    finally:
        if not failed:
            reference_time = flush_reference_time(state, now=now_fn(), allow_wall_clock=True)
            if flush_ready_closed_candles(producer, redis_client, redis_keys, state, topics, reference_time=reference_time, log_every_n=log_every_n):
                producer.flush()
            if not enable_auto_commit:
                commit_consumer(consumer)


def commit_consumer(consumer):
    commit = getattr(consumer, "commit", None)
    if callable(commit):
        commit()


def flush_reference_time(state, now=None, allow_wall_clock=False):
    max_event_time = state.window_builder.max_event_time
    if max_event_time is None:
        return now
    if not allow_wall_clock or now is None:
        return max_event_time
    if abs((now - max_event_time).total_seconds()) <= 3600:
        return max(now, max_event_time)
    return max_event_time


def process_raw_envelope(envelope, producer, redis_client, redis_keys, state, topics, log_every_n=500):
    topics = normalize_processor_topics(topics)
    channel = envelope.get("channel")
    feed_guard_result = enforce_active_feed(redis_client, redis_keys, envelope, cache=getattr(state, "active_feed_cache", None))
    if feed_guard_result != "accepted":
        write_processor_health(redis_client, redis_keys, envelope, result=feed_guard_result, state=state)
        return feed_guard_result

    if state.deduper.is_duplicate(envelope.get("sourceEventId")):
        print(f"중복 Raw event 제외: sourceEventId={envelope.get('sourceEventId')}", flush=True)
        write_processor_health(redis_client, redis_keys, envelope, result="duplicate", state=state)
        return "duplicate"

    if channel == "trades":
        trade = normalize_trade(envelope)
        publish_processed(producer, topics["trades"], {**trade, "layer": "trades"}, log_every_n)
        if topics.get("tick_fanout_enabled"):
            publish_tick_fanout(producer, topics["tick_fanout"], trade, log_every_n)
        write_trade_to_redis(redis_client, redis_keys, trade, state=state)
        result = process_trade_live_path(
            trade,
            producer,
            redis_client,
            redis_keys,
            state,
            topics,
            log_every_n=log_every_n,
        )
        write_processor_health(redis_client, redis_keys, envelope, result=result, state=state)
        return result

    if channel == "tickFanout":
        trade = normalized_trade_from_fanout(envelope)
        result = process_trade_live_path(
            trade,
            producer,
            redis_client,
            redis_keys,
            state,
            topics,
            log_every_n=log_every_n,
        )
        write_processor_health(redis_client, redis_keys, envelope, result=result, state=state)
        return result

    if channel == "quotes":
        quote = normalize_quote(envelope)
        update_order_flow_quote_cache(state, quote)
        if getattr(state, "order_flow_quote_cache_only", False):
            write_processor_health(redis_client, redis_keys, envelope, result="quotes_cached", state=state)
            return "quotes_cached"
        write_quote_to_redis(redis_client, redis_keys, quote, state=state)
        publish_processed(producer, topics["quotes"], quote, log_every_n)
        maybe_publish_quote_chart_event(redis_client, redis_keys, quote, state=state)
        write_processor_health(redis_client, redis_keys, envelope, result="quotes", state=state)
        return "quotes"

    if channel in {"bars", "updatedBars", "dailyBars"}:
        candle = normalize_bar(envelope, correction_type="UPDATED" if channel == "updatedBars" else "NONE")
        publish_closed_candle(producer, redis_client, redis_keys, state, topics, candle, log_every_n=log_every_n)
        if channel == "updatedBars" and candle["interval"] == "1m":
            publish_corrected_intraday_aggregates(
                producer,
                redis_client,
                redis_keys,
                state,
                topics,
                candle,
                log_every_n=log_every_n,
            )
        result = f"{channel}_confirmed_replace"
        write_processor_health(redis_client, redis_keys, envelope, result=result, state=state)
        return result

    if channel in {"statuses", "events"}:
        status = normalize_status(envelope)
        publish_processed(producer, topics["events"], {**status, "layer": "events"}, log_every_n)
        write_status_to_redis(redis_client, redis_keys, status)
        if status.get("symbol") and status.get("symbol") != "_MARKET":
            write_event_to_redis(redis_client, redis_keys, status)
        publish_chart_event(redis_client, redis_keys, market_status_event(status))
        write_processor_health(redis_client, redis_keys, envelope, result=channel, state=state)
        return channel

    if channel in {"corrections", "cancelErrors"}:
        print(f"Raw correction/cancel event 격리: channel={channel}", flush=True)
        write_processor_health(redis_client, redis_keys, envelope, result=f"{channel}_quarantined", state=state)
        return f"{channel}_quarantined"

    print(f"처리하지 않는 Raw channel입니다: {channel}", flush=True)
    write_processor_health(redis_client, redis_keys, envelope, result="ignored", state=state)
    return "ignored"


def process_trade_live_path(trade, producer, redis_client, redis_keys, state, topics, log_every_n=500):
    if trade_bucket_blocked_by_closed_watermark(redis_client, redis_keys, trade):
        return "trades_blocked_by_closed_watermark"
    accepted_for_window = state.window_builder.update(trade)
    live_candle = state.live_builder.update(trade) if accepted_for_window else None
    if live_candle:
        publish_live_candle(
            producer,
            redis_client,
            redis_keys,
            topics,
            live_candle,
            feed=trade.get("feed") or "unknown",
            log_every_n=log_every_n,
            throttle=getattr(state, "live_publish_throttle", None),
        )
    process_order_flow_live_path(trade, redis_client, redis_keys, state)
    if live_candle:
        publish_derived_live_candles(producer, redis_client, redis_keys, state, topics, trade["symbol"], live_1m=live_candle, log_every_n=log_every_n)
    return "trades" if accepted_for_window else "trades_late_after_closed"


def process_order_flow_live_path(trade, redis_client, redis_keys, state):
    builder = getattr(state, "order_flow_builder", None)
    symbol = str(trade.get("symbol") or "").upper()
    if builder is None or symbol not in builder.pinned_symbols:
        return None
    quote_cache = getattr(state, "order_flow_quote_cache", None)
    quote = quote_cache.quote_for(trade["symbol"]) if quote_cache is not None else None
    side = classify_trade_side(
        trade,
        quote,
        max_quote_age_ms=getattr(state, "order_flow_quote_max_age_ms", None),
        future_tolerance_ms=getattr(state, "order_flow_quote_future_tolerance_ms", 0),
    )
    close_previous_order_flow_minute_before_update(redis_client, redis_keys, state, trade)
    of_bin = builder.update(trade, side)
    if of_bin is None:
        return None
    write_order_flow_bin_to_redis(redis_client, redis_keys, of_bin, state=state)
    maybe_publish_order_flow_event(redis_client, redis_keys, state, of_bin)
    return of_bin


def update_order_flow_quote_cache(state, quote):
    quote_cache = getattr(state, "order_flow_quote_cache", None)
    if quote_cache is None or not callable(getattr(quote_cache, "update", None)):
        return None
    return quote_cache.update(quote)


def close_previous_order_flow_minute_before_update(redis_client, redis_keys, state, trade):
    builder = getattr(state, "order_flow_builder", None)
    symbol = str(trade.get("symbol") or "").upper()
    if builder is None or symbol not in builder.pinned_symbols:
        return False
    flush_state = _order_flow_redis_flush_state(state)
    entry = dict(flush_state.get(symbol) or {})
    previous_minute = entry.get("minute")
    if not previous_minute:
        return False
    try:
        minute_dt = floor_minute(trade["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    event_minute = to_iso(minute_dt)
    if previous_minute == event_minute:
        return False
    current_session = builder.current_session_date(symbol)
    incoming_session = minute_dt.astimezone(ORDER_FLOW_MARKET_TIMEZONE).date().isoformat()
    if current_session and current_session != incoming_session:
        return False
    if write_closed_order_flow_minute_to_redis(redis_client, redis_keys, symbol, previous_minute, builder, state=state):
        entry["preClosedMinute"] = previous_minute
        flush_state[symbol] = entry
        return True
    return False


def trade_bucket_blocked_by_closed_watermark(redis_client, redis_keys, trade):
    try:
        bucket_candle = {
            "symbol": trade["symbol"],
            "interval": "1m",
            "timestamp": to_iso(floor_minute(trade["timestamp"])),
        }
        return candle_at_or_before_watermark(bucket_candle, read_closed_candle_watermark(redis_client, redis_keys, trade["symbol"], "1m"))
    except Exception as exc:
        print(f"Closed watermark trade guard skipped: symbol={trade.get('symbol')} error={exc}", flush=True)
        return False


def flush_ready_closed_candles(producer, redis_client, redis_keys, state, topics, reference_time=None, log_every_n=500):
    topics = normalize_processor_topics(topics)
    published = 0
    ready_1m = state.window_builder.flush_ready(reference_time)
    for candle in ready_1m:
        publish_closed_candle(producer, redis_client, redis_keys, state, topics, candle, log_every_n=log_every_n)
        published += 1

        for interval_minutes in (5, 10, 60, 240):
            aggregated = state.aggregator.update(candle, interval_minutes)
            if aggregated:
                publish_closed_candle(producer, redis_client, redis_keys, state, topics, aggregated, log_every_n=log_every_n)
                published += 1

        state.daily_aggregator.update(candle)

    for daily in state.daily_aggregator.flush_ready(reference_time):
        publish_closed_candle(producer, redis_client, redis_keys, state, topics, daily, log_every_n=log_every_n)
        published += 1
        state.weekly_aggregator.update(daily)
        state.monthly_aggregator.update(daily)

    for weekly in state.weekly_aggregator.flush_ready(reference_time):
        publish_closed_candle(producer, redis_client, redis_keys, state, topics, weekly, log_every_n=log_every_n)
        published += 1

    for monthly in state.monthly_aggregator.flush_ready(reference_time):
        publish_closed_candle(producer, redis_client, redis_keys, state, topics, monthly, log_every_n=log_every_n)
        published += 1

    return published


def clickhouse_canonical_candle_loader(provider=None):
    if provider is None:
        from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider
        provider = ClickHouseMarketDataProvider()

    def load(symbol, from_time, to_time):
        return provider.candles(
            symbol,
            "1m",
            limit=500,
            from_time=to_iso(from_time),
            to_time=to_iso(to_time),
        )

    return load


def publish_corrected_intraday_aggregates(
    producer,
    redis_client,
    redis_keys,
    state,
    topics,
    corrected_candle,
    log_every_n=500,
):
    loader = getattr(state, "canonical_candle_loader", None)
    if not callable(loader):
        return 0
    anchor = parse_time(corrected_candle["timestamp"])
    from_time = anchor.replace(hour=(anchor.hour // 4) * 4, minute=0, second=0, microsecond=0)
    to_time = from_time + timedelta(minutes=240)
    try:
        source_candles = loader(corrected_candle["symbol"], from_time, to_time) or []
    except Exception as exc:
        state.correction_recompute_failures += 1
        print(
            f"Canonical correction recompute skipped: symbol={corrected_candle.get('symbol')} "
            f"timestamp={corrected_candle.get('timestamp')} error={exc}",
            flush=True,
        )
        return 0

    published = 0
    for interval_minutes in (5, 10, 60, 240):
        aggregated = state.aggregator.recompute(corrected_candle, interval_minutes, source_candles)
        if aggregated is None:
            continue
        publish_closed_candle(
            producer,
            redis_client,
            redis_keys,
            state,
            topics,
            aggregated,
            log_every_n=log_every_n,
        )
        published += 1
    return published


def normalize_processor_topics(topics):
    if "trades" in topics and "closed_candles" in topics:
        return {"tick_fanout_enabled": False, **topics}
    closed_topic = topics.get("closed_candles") or default_closed_candle_topics()
    trades_topic = topics.get("trades") or "market.layer.trades.v1"
    events_topic = topics.get("events") or topics.get("status") or "market.layer.events.v1"
    return {
        **topics,
        "trades": trades_topic,
        "quotes": topics.get("quotes") or "market.layer.quotes.v1",
        "tick_fanout": topics.get("tick_fanout") or {
            "1m": "market.realtime.ticks.to.1m.v1",
            "5m": "market.realtime.ticks.to.5m.v1",
            "10m": "market.realtime.ticks.to.10m.v1",
            "1D": "market.realtime.ticks.to.1d.v1",
            "1W": "market.realtime.ticks.to.1w.v1",
            "1M": "market.realtime.ticks.to.1mo.v1",
        },
        "closed_candles": closed_topic,
        "events": events_topic,
        "status": topics.get("status") or events_topic,
        "tick_fanout_enabled": bool(topics.get("tick_fanout_enabled")),
    }


def publish_closed_candle(producer, redis_client, redis_keys, state, topics, candle, log_every_n=500):
    topics = normalize_processor_topics(topics)
    candle = normalize_candle_numeric_fields(candle)
    candle = state.ma_state.attach_ma(candle)
    publish_processed(producer, candle_topic(topics["closed_candles"], candle["interval"]), {**candle, "layer": "candles", "state": "closed"}, log_every_n)
    write_closed_candle_to_redis(redis_client, redis_keys, candle)
    publish_chart_event(
        redis_client,
        redis_keys,
        websocket_event("CANDLE_CLOSED", candle["symbol"], candle["interval"], candle, feed=candle.get("feed") or "unknown"),
    )
    state.provisional_state.record_closed(candle)
    if candle["interval"] == "1m":
        publish_derived_live_candles(producer, redis_client, redis_keys, state, topics, candle["symbol"], anchor_1m_timestamp=candle["timestamp"], log_every_n=log_every_n)
    elif candle["interval"] == "1D":
        publish_daily_derived_live_candles(producer, redis_client, redis_keys, state, topics, candle["symbol"], anchor_1d_timestamp=candle["timestamp"], log_every_n=log_every_n)
    return candle


def publish_tick_fanout(producer, topics_by_interval, trade, log_every_n=500, skip_topic=None):
    for interval, topic in topics_by_interval.items():
        if topic == skip_topic:
            continue
        source_event_id = trade.get("sourceEventId") or f"trade/{trade['symbol']}/{trade['timestamp']}/{trade.get('tradeId') or 'unknown'}"
        payload = {
            **trade,
            "channel": "tickFanout",
            "fanoutInterval": interval,
            "layer": "trades",
            "sourceEventId": f"{source_event_id}/fanout/{interval}",
        }
        publish_processed(producer, topic, payload, log_every_n)


def candle_topic(topics_by_interval, interval):
    if topics_by_interval is None:
        return None
    if isinstance(topics_by_interval, str):
        return topics_by_interval
    if interval in topics_by_interval:
        return topics_by_interval[interval]
    normalized = {"1d": "1D", "1w": "1W", "1mo": "1M", "1MO": "1M"}.get(str(interval), interval)
    return topics_by_interval.get(normalized)


def normalized_trade_from_fanout(payload):
    if payload.get("raw"):
        return normalize_trade(payload)
    return {
        "eventType": "TRADE",
        "symbol": payload["symbol"],
        "tradeId": payload.get("tradeId"),
        "price": payload.get("price"),
        "size": payload.get("size"),
        "exchange": payload.get("exchange"),
        "conditions": payload.get("conditions", []),
        "tape": payload.get("tape"),
        "timestamp": payload.get("timestamp"),
        "source": payload.get("source") or "alpaca",
        "feed": payload.get("feed"),
        "feedProfile": payload.get("feedProfile"),
        "marketSession": payload.get("marketSession"),
        "sourceEventId": payload.get("sourceEventId"),
        "receivedAt": payload.get("receivedAt"),
    }


def publish_processed(producer, topic, payload, log_every_n=500):
    if not topic:
        return
    key = payload.get("symbol", "UNKNOWN")
    producer.send(topic, key=key, value=payload)
    # 로컬 테스트에서는 tick 수가 많아서 매 건 로그를 찍으면 처리 속도가 크게 느려집니다.
    # Kafka/Redis 전송은 계속 수행하고, 로그만 PROCESSOR_LOG_EVERY_N 건마다 줄여서 출력합니다.
    _PUBLISH_COUNTS[topic] += 1
    if _PUBLISH_COUNTS[topic] % log_every_n == 0:
        print(f"Processed Kafka 전송: topic={topic}, key={key}, count={_PUBLISH_COUNTS[topic]}", flush=True)


def write_trade_to_redis(redis_client, redis_keys, trade, state=None):
    symbol = trade["symbol"]
    if not _throttle_allows(state, "trade_redis_write_state", symbol, trade_redis_write_min_interval_ms_from_env()):
        return False
    key = redis_keys.live_trade(trade["symbol"])
    _redis_hset_expire(redis_client, key, {
        "symbol": trade["symbol"],
        "layer": "trades",
        "price": trade["price"],
        "size": trade.get("size") or 0,
        "timestamp": trade["timestamp"],
        "source": "alpaca.trades",
        "feed": trade.get("feed") or "unknown",
        "feedProfile": trade.get("feedProfile") or trade.get("feed") or "unknown",
        "marketSession": trade.get("marketSession") or "unknown",
    }, live_trade_ttl_seconds())
    return True


def write_quote_to_redis(redis_client, redis_keys, quote, state=None):
    symbol = quote["symbol"]
    if not _throttle_allows(state, "quote_redis_write_state", symbol, quote_redis_write_min_interval_ms_from_env()):
        return False
    key = redis_keys.live_quote(quote["symbol"])
    _redis_set_ex(redis_client, key, json.dumps(quote, ensure_ascii=False, separators=(",", ":")), 300)
    return True


def write_event_to_redis(redis_client, redis_keys, event):
    key = redis_keys.live_event(event["symbol"])
    _redis_set_ex(redis_client, key, json.dumps(event, ensure_ascii=False, separators=(",", ":")), 86400)


def read_closed_candle_watermark(redis_client, redis_keys, symbol, interval):
    watermark = redis_client.get(redis_keys.closed_candle_watermark(symbol, interval))
    if watermark:
        return watermark
    candle = read_latest_closed_candle(redis_client, redis_keys, symbol, interval)
    return candle_watermark_value(candle)


def read_latest_closed_candle(redis_client, redis_keys, symbol, interval):
    latest = redis_client.get(redis_keys.latest_closed_candle(symbol, interval))
    if not latest:
        return None
    try:
        return json.loads(latest)
    except (TypeError, json.JSONDecodeError):
        return None


def write_closed_candle_watermark_to_redis(redis_client, redis_keys, candle):
    value = candle_watermark_value(candle)
    if not value:
        return None
    key = redis_keys.closed_candle_watermark(candle["symbol"], candle["interval"])
    existing = redis_client.get(key)
    watermark = latest_watermark_value(existing, value)
    if watermark_after(existing, value):
        redis_client.set(key, value)
    redis_client.expire(key, 604800)
    return watermark


def delete_stale_live_candle(redis_client, redis_keys, symbol, interval, watermark, latest_closed_candle=None):
    key = redis_keys.live_candle(symbol, interval)
    value = redis_client.get(key)
    if not value:
        return False
    try:
        candle = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not live_candle_at_or_before_watermark(candle, watermark, latest_closed_candle):
        return False
    redis_client.delete(key)
    return True


def write_live_candle_to_redis(redis_client, redis_keys, candle):
    interval = candle.get("interval", "1m")
    latest_closed_candle = read_latest_closed_candle(redis_client, redis_keys, candle["symbol"], interval)
    watermark = read_closed_candle_watermark(redis_client, redis_keys, candle["symbol"], interval)
    if live_candle_at_or_before_watermark(candle, watermark, latest_closed_candle):
        redis_client.delete(redis_keys.live_candle(candle["symbol"], candle.get("interval", "1m")))
        return False
    key = redis_keys.live_candle(candle["symbol"], interval)
    redis_client.set(key, json.dumps(candle, ensure_ascii=False, separators=(",", ":")))
    redis_client.expire(key, live_candle_ttl_seconds())
    return True


def publish_live_candle(producer, redis_client, redis_keys, topics, candle, feed="unknown", log_every_n=500, throttle=None):
    if throttle is not None and not throttle.should_publish(candle):
        return False
    if not write_live_candle_to_redis(redis_client, redis_keys, candle):
        return False
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
    return True


def publish_derived_live_candles(producer, redis_client, redis_keys, state, topics, symbol, live_1m=None, anchor_1m_timestamp=None, log_every_n=500):
    provisional_1d = None
    for interval in ("5m", "10m", "1h", "4h", "1D"):
        candle = state.provisional_state.build_from_1m(
            symbol,
            interval,
            anchor_timestamp=anchor_1m_timestamp,
            live_1m=live_1m,
        )
        if not candle:
            continue
        publish_live_candle(
            producer,
            redis_client,
            redis_keys,
            topics,
            candle,
            feed=candle.get("feed") or "unknown",
            log_every_n=log_every_n,
            throttle=getattr(state, "live_publish_throttle", None),
        )
        if interval == "1D":
            provisional_1d = candle
    if provisional_1d:
        publish_daily_derived_live_candles(producer, redis_client, redis_keys, state, topics, symbol, provisional_1d=provisional_1d, log_every_n=log_every_n)


def publish_daily_derived_live_candles(producer, redis_client, redis_keys, state, topics, symbol, provisional_1d=None, anchor_1d_timestamp=None, log_every_n=500):
    for interval in ("1W", "1M"):
        candle = state.provisional_state.build_from_1d(
            symbol,
            interval,
            anchor_timestamp=anchor_1d_timestamp,
            provisional_1d=provisional_1d,
        )
        if candle:
            publish_live_candle(
                producer,
                redis_client,
                redis_keys,
                topics,
                candle,
                feed=candle.get("feed") or "unknown",
                log_every_n=log_every_n,
                throttle=getattr(state, "live_publish_throttle", None),
            )


def write_closed_candle_to_redis(redis_client, redis_keys, candle):
    candle = normalize_candle_numeric_fields(candle)
    latest_key = redis_keys.latest_closed_candle(candle["symbol"], candle["interval"])
    series_key = redis_keys.recent_candles(candle["symbol"], candle["interval"])
    candle_json = json.dumps(candle, ensure_ascii=False, separators=(",", ":"))
    score = timestamp_score(candle["timestamp"])
    pending_key = redis_keys.pending_replace_candle(candle["symbol"], candle["interval"], candle["timestamp"])
    redis_client.set(pending_key, candle_json)
    redis_client.expire(pending_key, 3600)
    redis_client.set(latest_key, candle_json)
    redis_client.zremrangebyscore(series_key, score, score)
    redis_client.zadd(series_key, {candle_json: score})
    cap = min(redis_closed_candle_cap(candle["interval"]), 120)
    redis_client.zremrangebyrank(series_key, 0, -cap - 1)
    redis_client.expire(latest_key, 86400)
    redis_client.expire(series_key, 604800)
    watermark = write_closed_candle_watermark_to_redis(redis_client, redis_keys, candle)
    delete_stale_live_candle(redis_client, redis_keys, candle["symbol"], candle["interval"], watermark, candle)


def write_status_to_redis(redis_client, redis_keys, status):
    status_json = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
    _redis_set_ex(redis_client, redis_keys.market_status_latest(), status_json, 86400)
    symbol = status.get("symbol")
    if symbol and symbol != "_MARKET":
        key = redis_keys.market_status_symbol_latest(symbol)
        _redis_set_ex(redis_client, key, status_json, 86400)


def write_order_flow_bin_to_redis(redis_client, redis_keys, of_bin, state=None):
    builder = getattr(state, "order_flow_builder", None)
    symbol = str(of_bin["symbol"]).upper()
    event_minute = of_bin["eventMinute"]
    flush_state = _order_flow_redis_flush_state(state)
    entry = dict(flush_state.get(symbol) or {})

    if builder is not None and callable(getattr(builder, "consume_session_rollover", None)):
        if builder.consume_session_rollover(of_bin["symbol"]):
            _delete_order_flow_live_keys(redis_client, redis_keys, symbol)
            flush_state.pop(symbol, None)
            _order_flow_minutes_ttl_state(state).discard(symbol)
            entry = {}

    previous_minute = entry.get("minute")
    minute_changed = bool(previous_minute and previous_minute != event_minute)
    if minute_changed:
        if entry.get("preClosedMinute") != previous_minute:
            write_closed_order_flow_minute_to_redis(redis_client, redis_keys, symbol, previous_minute, builder, state=state)
        entry = {}

    now = time.monotonic()
    last_flush = float(entry.get("lastFlush", 0.0) or 0.0)
    force_flush = not entry or minute_changed
    due = (now - last_flush) * 1000 >= redis_flush_ms_from_env()
    if force_flush or due:
        if write_live_order_flow_minute_to_redis(redis_client, redis_keys, symbol, event_minute, builder, fallback_bin=of_bin):
            last_flush = now

    flush_state[symbol] = {"minute": event_minute, "lastFlush": last_flush}


def write_live_order_flow_minute_to_redis(redis_client, redis_keys, symbol, event_minute, builder=None, *, fallback_bin=None):
    bins = _order_flow_bins_for_minute(builder, symbol, event_minute, fallback_bin=fallback_bin)
    if not bins:
        return False
    key = redis_keys.order_flow_live_minute(symbol)
    value = encode_order_flow_minute_blob(order_flow_minute_blob(symbol, event_minute, bins))
    _redis_set_ex(redis_client, key, value, live_minute_ttl_seconds_from_env())
    return True


def write_closed_order_flow_minute_to_redis(redis_client, redis_keys, symbol, event_minute, builder=None, state=None):
    bins = _order_flow_bins_for_minute(builder, symbol, event_minute)
    if not bins:
        return False
    key = redis_keys.order_flow_minutes(symbol)
    value = encode_order_flow_minute_blob(order_flow_minute_blob(symbol, event_minute, bins))
    redis_client.zadd(key, {value: order_flow_minute_score(event_minute)})
    if state is None:
        redis_client.expire(key, live_ttl_seconds_from_env())
        return True
    ttl_state = _order_flow_minutes_ttl_state(state)
    normalized_symbol = str(symbol or "").upper()
    if normalized_symbol not in ttl_state:
        redis_client.expire(key, live_ttl_seconds_from_env())
        ttl_state.add(normalized_symbol)
    return True


def _order_flow_bins_for_minute(builder, symbol, event_minute, *, fallback_bin=None):
    if builder is not None and callable(getattr(builder, "bins_for_minute", None)):
        return builder.bins_for_minute(symbol, event_minute)
    if fallback_bin and fallback_bin.get("eventMinute") == event_minute:
        return [fallback_bin]
    return []


def _order_flow_redis_flush_state(state):
    if state is None:
        return {}
    flush_state = getattr(state, "order_flow_redis_flush_state", None)
    if flush_state is None:
        state.order_flow_redis_flush_state = {}
        flush_state = state.order_flow_redis_flush_state
    return flush_state


def _order_flow_minutes_ttl_state(state):
    if state is None:
        return set()
    ttl_state = getattr(state, "order_flow_minutes_ttl_state", None)
    if ttl_state is None:
        state.order_flow_minutes_ttl_state = set()
        ttl_state = state.order_flow_minutes_ttl_state
    return ttl_state


def _delete_order_flow_live_keys(redis_client, redis_keys, symbol):
    redis_client.delete(redis_keys.order_flow_minutes(symbol))
    redis_client.delete(redis_keys.order_flow_live_minute(symbol))


def _redis_set_ex(redis_client, key, value, ttl_seconds):
    try:
        return redis_client.set(key, value, ex=ttl_seconds)
    except TypeError:
        redis_client.set(key, value)
        return redis_client.expire(key, ttl_seconds)


def _redis_hset_expire(redis_client, key, mapping, ttl_seconds):
    pipeline_factory = getattr(redis_client, "pipeline", None)
    if callable(pipeline_factory):
        try:
            pipe = pipeline_factory(transaction=False)
        except TypeError:
            pipe = pipeline_factory()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, ttl_seconds)
        return pipe.execute()
    redis_client.hset(key, mapping=mapping)
    return redis_client.expire(key, ttl_seconds)


def _throttle_allows(state, attr, key, min_interval_ms):
    if state is None or min_interval_ms <= 0:
        return True
    throttle_state = getattr(state, attr, None)
    if throttle_state is None:
        throttle_state = {}
        setattr(state, attr, throttle_state)
    normalized_key = str(key or "_UNKNOWN").upper()
    now = time.monotonic()
    previous = throttle_state.get(normalized_key)
    if previous is not None and (now - float(previous)) * 1000 < min_interval_ms:
        return False
    throttle_state[normalized_key] = now
    return True


def maybe_publish_order_flow_event(redis_client, redis_keys, state, of_bin):
    builder = getattr(state, "order_flow_builder", None)
    if builder is None:
        return
    publish_state = getattr(state, "order_flow_publish_state", None)
    if publish_state is None:
        state.order_flow_publish_state = {}
        publish_state = state.order_flow_publish_state
    symbol = of_bin["symbol"]
    event_minute = of_bin["eventMinute"]
    now = time.monotonic()
    entry = publish_state.get(symbol)
    minute_changed = entry is not None and entry.get("minute") != event_minute
    throttled = entry is not None and (now - float(entry.get("lastPublish", 0))) * 1000 < publish_throttle_ms_from_env()
    if throttled and not minute_changed:
        return
    seq = int(entry.get("seq", 0)) if entry is not None else 0
    if minute_changed:
        previous_minute = entry.get("minute")
        previous_bins = builder.bins_for_minute(symbol, previous_minute)
        seq += 1
        publish_chart_event(redis_client, redis_keys, order_flow_event(symbol, previous_minute, previous_bins, sequence=seq))
    current_bins = builder.bins_for_minute(symbol, event_minute)
    seq += 1
    publish_chart_event(redis_client, redis_keys, order_flow_event(symbol, event_minute, current_bins, sequence=seq))
    publish_state[symbol] = {"lastPublish": now, "minute": event_minute, "seq": seq}


def maybe_publish_quote_chart_event(redis_client, redis_keys, quote, state=None):
    if not _throttle_allows(state, "quote_event_publish_state", quote["symbol"], quote_event_publish_min_interval_ms_from_env()):
        return False
    publish_chart_event(redis_client, redis_keys, quote_event(quote))
    return True


def publish_chart_event(redis_client, redis_keys, event):
    event_json = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    redis_client.publish(redis_keys.market_events(), event_json)


def quote_event(quote):
    timestamp = quote.get("timestamp") or quote.get("receivedAt") or "unknown"
    symbol = quote["symbol"]
    return {
        "type": "QUOTE_UPDATE",
        "layer": "quotes",
        "eventId": f"delta/QUOTE_UPDATE/{symbol}/{timestamp}/{quote.get('sourceEventId') or 'unknown'}",
        "cursor": f"v1:{symbol}:quotes:{timestamp}:{quote.get('sourceEventId') or 'unknown'}",
        "symbol": symbol,
        "interval": "quotes",
        "source": quote.get("source") or "alpaca.quotes",
        "feed": quote.get("feed") or "unknown",
        "feedProfile": quote.get("feedProfile"),
        "marketSession": quote.get("marketSession"),
        "data": quote,
    }


def enforce_active_feed(redis_client, redis_keys, envelope, cache=None):
    active = cache.get(lambda: read_active_feed(redis_client, redis_keys)) if cache else read_active_feed(redis_client, redis_keys)
    if not active:
        return "accepted"
    expected_profile = active.get("activeFeedProfile") or active.get("feedProfile") or active.get("profile")
    expected_epoch = active.get("epoch") or active.get("feedEpoch")
    actual_profile = envelope.get("feedProfile") or envelope.get("feed")
    actual_epoch = envelope.get("feedEpoch")
    symbol = envelope.get("symbol") or (envelope.get("raw") or {}).get("S") or "_UNKNOWN"
    if str(expected_profile or "").strip().lower() in {"", "none", "closed"}:
        return "accepted"
    if expected_profile and actual_profile and str(expected_profile).lower() != str(actual_profile).lower():
        quarantine_feed_payload(redis_client, redis_keys, actual_profile, symbol, envelope, "wrong_feed_profile")
        return "quarantined_wrong_feed_profile"
    if expected_epoch and actual_epoch and str(expected_epoch) != str(actual_epoch):
        quarantine_feed_payload(redis_client, redis_keys, actual_profile or expected_profile or "unknown", symbol, envelope, "stale_feed_epoch")
        return "quarantined_stale_feed_epoch"
    return "accepted"


def read_active_feed(redis_client, redis_keys):
    value = None
    try:
        value = redis_client.get(redis_keys.feed_active())
    except Exception:
        value = None
    if value:
        try:
            return json.loads(value)
        except Exception:
            return {"activeFeedProfile": value}
    try:
        profile = redis_client.get(redis_keys.feed_active_profile())
        epoch = redis_client.get(redis_keys.feed_active_epoch())
    except Exception:
        return {}
    result = {}
    if profile:
        result["activeFeedProfile"] = profile
    if epoch:
        result["epoch"] = epoch
    return result


def quarantine_feed_payload(redis_client, redis_keys, feed_profile, symbol, envelope, reason):
    try:
        event_time = envelope.get("eventTime") or envelope.get("receivedAt") or (envelope.get("raw") or {}).get("t")
        quarantine_date = str(event_time).split("T", 1)[0] if event_time else datetime.now(timezone.utc).date().isoformat()
        key = redis_keys.feed_quarantine(quarantine_date)
        redis_client.lpush(key, json.dumps({
            "reason": reason,
            "feedProfile": feed_profile or "unknown",
            "symbol": symbol,
            "payload": envelope,
        }, ensure_ascii=False, separators=(",", ":")))
        redis_client.ltrim(key, 0, 99)
        redis_client.expire(key, 86400)
    except Exception as exc:
        print(f"Feed quarantine write skipped: reason={reason} error={exc}", flush=True)


def write_processor_health(redis_client, redis_keys, envelope, result, state=None):
    if not _throttle_allows(state, "health_write_state", "market-processor", health_write_min_interval_ms_from_env()):
        return False
    try:
        health_fields = {
            "status": "ok",
            "lastResult": result,
            "lastChannel": envelope.get("channel"),
            "lastSymbol": envelope.get("symbol") or (envelope.get("raw") or {}).get("S"),
            "lastEventTime": envelope.get("eventTime") or (envelope.get("raw") or {}).get("t"),
            "lastFeed": envelope.get("feed"),
            "lastFeedProfile": envelope.get("feedProfile"),
            "lastMarketSession": envelope.get("marketSession"),
            "lastSourceEventId": envelope.get("sourceEventId"),
            "stateSizes": processor_state_sizes(state),
        }
        write_component_health(
            redis_client,
            redis_keys,
            "market-processor",
            **health_fields,
        )
        symbol = health_fields.get("lastSymbol")
        if symbol:
            write_component_health(
                redis_client,
                redis_keys,
                f"market-processor:symbol:{str(symbol).upper()}",
                **health_fields,
            )
        feed_profile = health_fields.get("lastFeedProfile")
        if feed_profile:
            write_component_health(
                redis_client,
                redis_keys,
                f"market-processor:feed:{str(feed_profile).lower()}",
                **health_fields,
            )
        return True
    except Exception as exc:
        print(f"Processor health heartbeat write skipped: error={exc}", flush=True)
        return False


def processor_state_sizes(state):
    if state is None:
        return {}
    provisional = getattr(getattr(state, "provisional_state", None), "closed", {})
    aggregate_windows = getattr(getattr(state, "aggregator", None), "windows", {})
    calendar_aggregators = [
        getattr(state, "daily_aggregator", None),
        getattr(state, "weekly_aggregator", None),
        getattr(state, "monthly_aggregator", None),
    ]
    live_builder = getattr(state, "live_builder", None)
    window_builder = getattr(state, "window_builder", None)
    aggregator = getattr(state, "aggregator", None)
    moving_average = getattr(state, "ma_state", None)
    return {
        "liveCandles": len(getattr(live_builder, "candles", {})),
        "tickWindows": len(getattr(window_builder, "windows", {})),
        "tickClosedKeys": len(getattr(window_builder, "closed_keys", ())),
        "provisionalCandles": sum(len(rows) for rows in provisional.values()),
        "aggregateWindows": len(aggregate_windows),
        "aggregateRows": sum(len(rows) for rows in aggregate_windows.values()),
        "aggregateClosedKeys": len(getattr(aggregator, "closed_keys", ())),
        "calendarWindows": sum(len(getattr(builder, "windows", {})) for builder in calendar_aggregators if builder is not None),
        "calendarClosedKeys": sum(len(getattr(builder, "closed_keys", ())) for builder in calendar_aggregators if builder is not None),
        "movingAverageCloses": sum(len(rows) for rows in getattr(moving_average, "closes", {}).values()),
        "dedupeEntries": len(getattr(getattr(state, "deduper", None), "seen", ())),
        "evictions": {
            "liveCandles": getattr(live_builder, "evictions", 0),
            "movingAverageCloses": getattr(moving_average, "evictions", 0),
            "tickClosedKeys": getattr(getattr(window_builder, "closed_keys", None), "evictions", 0),
            "aggregateClosedKeys": getattr(getattr(aggregator, "closed_keys", None), "evictions", 0),
            "calendarClosedKeys": sum(
                getattr(getattr(builder, "closed_keys", None), "evictions", 0)
                for builder in calendar_aggregators
                if builder is not None
            ),
        },
        "recomputes": {
            "aggregateWindows": getattr(aggregator, "recomputes", 0),
            "failures": getattr(state, "correction_recompute_failures", 0),
        },
    }


def timestamp_score(timestamp):
    from datetime import datetime
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def live_trade_ttl_seconds():
    default_ttl = parse_positive_int(os.getenv("LIVE_CANDLE_TTL_SECONDS", "180"), default=180)
    return parse_positive_int(os.getenv("LIVE_TRADE_TTL_SECONDS", str(default_ttl)), default=default_ttl)


def live_candle_ttl_seconds():
    return parse_positive_int(os.getenv("LIVE_CANDLE_TTL_SECONDS", "180"), default=180)


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


def parse_non_negative_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def parse_bool(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
