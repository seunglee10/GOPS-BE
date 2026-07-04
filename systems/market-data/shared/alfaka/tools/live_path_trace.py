# 역할: 한 종목 기준으로 Alpaca live path의 read-only 운영 증거를 수집합니다.
# 사용: 로컬 compose 또는 AWS 배포 runtime shell에서 input Kafka -> Redis/API 상태를 추적합니다.
import argparse
import json
import os
from datetime import datetime, timezone

import requests

from alfaka.alpaca.feed_profiles import (
    configured_closed_dates,
    configured_feed_profiles,
    feed_profile_active_for_session,
    market_session_for_datetime,
)
from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.common.symbols import is_crypto_symbol


DEFAULT_INTERVAL = "1m"


def expected_raw_topics(raw_prefix):
    return [
        "market.input.realtime.trades.v1",
        "market.input.realtime.quotes.v1",
        "market.input.realtime.events.v1",
        "market.input.realtime.bars.1m.v1",
        "market.input.realtime.updated-bars.1m.v1",
        "market.input.realtime.daily-bars.v1",
    ]


def expected_processed_topics(environ=None):
    environ = os.environ if environ is None else environ
    configured = parse_csv(environ.get("KAFKA_PROCESSED_TOPICS", ""))
    canonical = [
        environ.get("KAFKA_TRADES_LAYER_TOPIC", "market.layer.trades.v1"),
        environ.get("KAFKA_CLOSED_CANDLE_TOPIC", "market.layer.candles.closed.v1"),
        environ.get("KAFKA_LIVE_CANDLE_TOPIC", "market.layer.candles.live.v1"),
        environ.get("KAFKA_QUOTES_LAYER_TOPIC", "market.layer.quotes.v1"),
        environ.get("KAFKA_EVENTS_LAYER_TOPIC", "market.layer.events.v1"),
    ]
    return list(dict.fromkeys(configured + canonical))


def trace_check(name, status, **details):
    """개별 live path 점검 결과를 공통 dict 형태로 만듭니다."""
    return {"name": name, "status": status, "details": details}


def overall_status(checks):
    """여러 점검 결과를 하나의 ok/warn/fail 상태로 요약합니다."""
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def collect_trace(
    *,
    symbol,
    interval=DEFAULT_INTERVAL,
    api_base_url=None,
    redis_url=None,
    kafka_bootstrap_servers=None,
    processor_group_id=None,
    raw_prefix=None,
    require_live=False,
    timeout_seconds=5,
):
    """API, Redis, Kafka, ClickHouse/S3 경로가 살아 있는지 한 번에 점검합니다."""
    symbol = symbol.upper()
    interval = interval or DEFAULT_INTERVAL
    api_base_url = (api_base_url or os.getenv("GOPS_API_BASE_URL") or os.getenv("API_BASE_URL") or "http://localhost:8000").rstrip("/")
    redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kafka_bootstrap_servers = kafka_bootstrap_servers or os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    processor_group_id = processor_group_id or os.getenv("KAFKA_PROCESSOR_GROUP_ID") or "alfaka-market-processor"
    raw_prefix = raw_prefix or os.getenv("KAFKA_INPUT_TOPIC_PREFIX", "market.input")

    raw_topics = expected_raw_topics(raw_prefix)
    processed_topics = expected_processed_topics()
    checks = []
    checks.append(check_market_session(symbol))
    checks.append(check_api(api_base_url, symbol, interval, timeout_seconds))
    checks.append(check_redis(redis_url, symbol, interval, require_live))
    checks.append(check_kafka(kafka_bootstrap_servers, processor_group_id, raw_topics, processed_topics))

    return {
        "checkedAt": utc_now_iso(),
        "symbol": symbol,
        "interval": interval,
        "status": overall_status(checks),
        "path": "Alpaca -> input Kafka -> tick fanout topics -> Kubernetes market-processor -> Redis/layer Kafka -> ClickHouse/S3/API/WebSocket -> browser",
        "config": {
            "apiBaseUrl": api_base_url,
            "redisUrl": redact_url(redis_url),
            "kafkaBootstrapServers": kafka_bootstrap_servers,
            "processorGroupId": processor_group_id,
            "rawTopicPrefix": raw_prefix,
            "noDummyData": True,
        },
        "checks": checks,
    }


def check_market_session(symbol=None):
    """현재 장 상태와 심볼 종류를 보고 실시간 payload 기대 여부를 진단합니다."""
    try:
        now = datetime.now(timezone.utc)
        session = market_session_for_datetime(now)
        profiles = configured_feed_profiles()
        recommended_feed = recommended_realtime_feed_for_session(session, symbol=symbol)
        active_profiles = [
            profile.profile_id
            for profile in profiles
            if feed_profile_active_for_session(profile, session)
        ]
        closed_dates = sorted(configured_closed_dates())
        crypto_symbol = is_crypto_symbol(symbol)
        if session == "closed" and not crypto_symbol:
            status = "warn"
            note = "market session is closed; a live feed can authenticate but may not receive market payloads"
        elif active_profiles:
            status = "ok"
            note = "at least one configured feed profile is active for the current session"
        else:
            status = "warn"
            note = "no configured feed profile is active for the current session"
        return trace_check(
            "market-session",
            status,
            utcNow=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            session=session,
            recommendedFeed=recommended_feed,
            recommendedLocalIngestorService=local_ingestor_service_for_feed(recommended_feed),
            payloadExpected=(crypto_symbol or session != "closed") and bool(recommended_feed),
            configuredFeedProfiles=[profile.profile_id for profile in profiles],
            activeFeedProfiles=active_profiles,
            configuredClosedDates=closed_dates,
            note=note,
        )
    except Exception as exc:
        return trace_check("market-session", "fail", error=str(exc))


def recommended_realtime_feed_for_session(session, symbol=None):
    """세션과 심볼에 맞는 권장 Alpaca realtime feed 이름을 반환합니다."""
    if is_crypto_symbol(symbol):
        return "crypto"
    normalized = str(session or "").strip().lower()
    if normalized in {"pre", "regular", "after"}:
        return "sip"
    if normalized == "overnight":
        return "boats"
    return None


def local_ingestor_service_for_feed(feed):
    """feed 이름을 로컬 docker-compose ingestor 서비스 이름으로 매핑합니다."""
    if feed == "sip":
        return "alpaca-ingestor"
    if feed == "boats":
        return "alpaca-ingestor-boats"
    if feed == "crypto":
        return "alpaca-ingestor-crypto"
    return None


def check_api(api_base_url, symbol, interval, timeout_seconds):
    try:
        health = requests.get(f"{api_base_url}/health", timeout=timeout_seconds)
        candles = requests.get(
            f"{api_base_url}/api/charts/candles",
            params={"symbol": symbol, "interval": interval, "limit": 20},
            timeout=timeout_seconds,
        )
        symbols = requests.get(
            f"{api_base_url}/api/market/symbols",
            params={"page": 1, "pageSize": 5, "q": ""},
            timeout=timeout_seconds,
        )
        health_ok = health.ok
        candles_ok = candles.ok
        symbols_ok = symbols.ok
        payload = candles.json() if candles_ok else {}
        symbols_payload = symbols.json() if symbols_ok else {}
        status = "ok" if health_ok and candles_ok and symbols_ok and payload.get("candles") else "warn"
        return trace_check(
            "api",
            status,
            healthStatus=health.status_code,
            candlesStatus=candles.status_code,
            dataStatus=payload.get("dataStatus"),
            returnedCount=payload.get("returnedCount"),
            sourceInterval=payload.get("sourceInterval"),
            newestTimestamp=payload.get("newestTimestamp"),
            symbolsStatus=symbols.status_code,
            symbolsSource=symbols_payload.get("source"),
            symbolsTotal=symbols_payload.get("total"),
            symbolsSample=[item.get("symbol") for item in symbols_payload.get("symbols", [])],
        )
    except Exception as exc:
        return trace_check("api", "fail", error=str(exc))


def check_redis(redis_url, symbol, interval, require_live):
    try:
        import redis

        client = redis.from_url(redis_url, decode_responses=True)
        keys = RedisKeyBuilder()
        price_key = keys.price_latest(symbol)
        live_key = keys.live_candle(symbol, interval)
        latest_key = keys.latest_candle(symbol, interval)
        series_key = keys.recent_candles(symbol, interval)
        subscription_key = keys.subscription_symbols()
        subscription_symbol_key = keys.subscription_symbol(symbol)
        active_symbols_key = keys.active_symbols()
        active_chart_source_key = keys.subscription_source_active_chart(symbol)
        processor_health_key = keys.component_health("market-processor")
        price = client.hgetall(price_key) if client.exists(price_key) else {}
        live = client.get(live_key)
        latest = client.get(latest_key)
        series_count = client.zcard(series_key)
        subscription_symbols = sorted(client.smembers(subscription_key) or [])
        subscription_record = client.hgetall(subscription_symbol_key) if client.exists(subscription_symbol_key) else {}
        active_symbols = sorted(client.smembers(active_symbols_key) or [])
        active_chart_source_members = sorted(client.smembers(active_chart_source_key) or [])
        processor_health = parse_json_value(client.get(processor_health_key))
        has_runtime_evidence = bool(price or live or processor_health)
        if require_live and not live:
            status = "fail"
        elif not (has_runtime_evidence or latest or series_count):
            status = "warn"
        else:
            status = "ok"
        return trace_check(
            "redis",
            status,
            priceKey=price_key,
            pricePresent=bool(price),
            liveCandleKey=live_key,
            liveCandlePresent=bool(live),
            latestCandleKey=latest_key,
            latestCandlePresent=bool(latest),
            recentSeriesKey=series_key,
            recentSeriesCount=series_count,
            subscriptionSymbolsKey=subscription_key,
            subscriptionSymbolCount=len(subscription_symbols),
            subscriptionSymbolSample=subscription_symbols[:10],
            symbolSubscribed=symbol in subscription_symbols,
            subscriptionSymbolKey=subscription_symbol_key,
            subscriptionLayers=subscription_record.get("layers"),
            subscriptionSources=subscription_record.get("sources"),
            activeSymbolsKey=active_symbols_key,
            activeSymbolCount=len(active_symbols),
            activeSymbolSample=active_symbols[:10],
            activeChartSourceKey=active_chart_source_key,
            activeChartSourceCount=len(active_chart_source_members),
            activeChartSessionCount=subscription_record.get("activeChartSessionCount"),
            processorHealthKey=processor_health_key,
            processorHeartbeatPresent=bool(processor_health),
            processorUpdatedAt=(processor_health or {}).get("updatedAt"),
            processorLastChannel=(processor_health or {}).get("lastChannel"),
            processorLastSymbol=(processor_health or {}).get("lastSymbol"),
        )
    except Exception as exc:
        return trace_check("redis", "fail", error=str(exc), redisUrl=redact_url(redis_url))


def check_kafka(bootstrap_servers, processor_group_id, raw_topics, processed_topics):
    try:
        from kafka import KafkaConsumer, TopicPartition

        consumer = KafkaConsumer(
            bootstrap_servers=parse_csv(bootstrap_servers),
            group_id=processor_group_id,
            enable_auto_commit=False,
            consumer_timeout_ms=1000,
        )
        all_topics = consumer.topics()
        required_topics = raw_topics + processed_topics
        missing_topics = sorted(topic for topic in required_topics if topic not in all_topics)
        raw_partitions = []
        for topic in raw_topics:
            partitions = consumer.partitions_for_topic(topic) or []
            raw_partitions.extend(TopicPartition(topic, partition) for partition in partitions)
        lag = {}
        total_lag = 0
        if raw_partitions:
            consumer.assign(raw_partitions)
            end_offsets = consumer.end_offsets(raw_partitions)
            for partition in raw_partitions:
                committed = consumer.committed(partition)
                end_offset = end_offsets.get(partition, 0)
                partition_lag = None if committed is None else max(0, end_offset - committed)
                lag[f"{partition.topic}:{partition.partition}"] = {
                    "committed": committed,
                    "endOffset": end_offset,
                    "lag": partition_lag,
                }
                if partition_lag is not None:
                    total_lag += partition_lag
        consumer.close()
        if missing_topics:
            status = "fail"
        elif not raw_partitions:
            status = "warn"
        else:
            status = "ok"
        return trace_check(
            "kafka",
            status,
            processorGroupId=processor_group_id,
            missingTopics=missing_topics,
            rawTopicCount=len(raw_topics),
            processedTopicCount=len(processed_topics),
            rawPartitionCount=len(raw_partitions),
            totalRawLag=total_lag,
            rawLag=lag,
        )
    except Exception as exc:
        return trace_check("kafka", "fail", error=str(exc), processorGroupId=processor_group_id)


def redact_url(value):
    if not value:
        return value
    text = str(value)
    if "@" not in text:
        return text
    scheme, rest = text.split("://", 1) if "://" in text else ("", text)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def parse_json_value(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def render_human(trace):
    lines = [
        f"live path trace: symbol={trace['symbol']} interval={trace['interval']} status={trace['status']}",
        f"path: {trace['path']}",
        f"api: {trace['config']['apiBaseUrl']}",
        f"redis: {trace['config']['redisUrl']}",
        f"kafka: {trace['config']['kafkaBootstrapServers']} group={trace['config']['processorGroupId']}",
    ]
    for check in trace["checks"]:
        lines.append(f"- {check['name']}: {check['status']} {json.dumps(check['details'], ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="한 종목 기준 live market-data path를 read-only로 추적합니다.")
    parser.add_argument("symbol", nargs="?", default="NVDA", help="확인할 심볼입니다. 예: NVDA")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, choices=["1m", "5m", "10m", "1D", "1W", "1M"])
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--redis-url", default=None)
    parser.add_argument("--kafka-bootstrap-servers", default=None)
    parser.add_argument("--processor-group-id", default=None)
    parser.add_argument("--raw-prefix", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--require-live", action="store_true", help="현재가/live candle이 없으면 실패로 처리합니다.")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력합니다.")
    parser.add_argument("--strict", action="store_true", help="status가 ok가 아니면 exit 1을 반환합니다.")
    args = parser.parse_args()

    load_dotenv()
    trace = collect_trace(
        symbol=args.symbol,
        interval=args.interval,
        api_base_url=args.api_base_url,
        redis_url=args.redis_url,
        kafka_bootstrap_servers=args.kafka_bootstrap_servers,
        processor_group_id=args.processor_group_id,
        raw_prefix=args.raw_prefix,
        require_live=args.require_live,
        timeout_seconds=args.timeout_seconds,
    )
    if args.json:
        print(json.dumps(trace, indent=2, ensure_ascii=False))
    else:
        print(render_human(trace))
    if args.strict and trace["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
