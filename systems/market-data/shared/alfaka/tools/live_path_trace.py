# 역할: 한 종목 기준으로 Alpaca live path의 read-only 운영 증거를 수집합니다.
# 사용: 로컬 compose 또는 AWS 배포 runtime shell에서 raw Kafka -> Redis/API 상태를 추적합니다.
import argparse
import json
import os
from datetime import datetime, timezone

import requests

from alfaka.common.env import load_dotenv, parse_csv
from alfaka.common.redis_keys import RedisKeyBuilder


DEFAULT_INTERVAL = "1m"


def expected_raw_topics(raw_prefix):
    return [
        f"{raw_prefix}.bars",
        f"{raw_prefix}.updated-bars",
        f"{raw_prefix}.trades",
        f"{raw_prefix}.daily-bars",
        f"{raw_prefix}.statuses",
        f"{raw_prefix}.quotes",
        f"{raw_prefix}.corrections",
        f"{raw_prefix}.cancel-errors",
    ]


def expected_processed_topics(environ=None):
    environ = environ or os.environ
    configured = parse_csv(environ.get("KAFKA_PROCESSED_TOPICS", ""))
    canonical = [
        environ.get("KAFKA_TICKS_TOPIC", "market.ticks.v1"),
        environ.get("KAFKA_LIVE_CANDLE_TOPIC", "market.candles.live.1m.v1"),
        environ.get("KAFKA_CLOSED_CANDLE_TOPIC", "market.candles.closed.v1"),
        environ.get("KAFKA_STATUS_TOPIC", "market.status.v1"),
        environ.get("KAFKA_VOLUME_PROFILE_BINS_TOPIC", "market.volume-profile-bins.1m.v1"),
    ]
    return list(dict.fromkeys(configured + canonical))


def trace_check(name, status, **details):
    return {"name": name, "status": status, "details": details}


def overall_status(checks):
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
    symbol = symbol.upper()
    interval = interval or DEFAULT_INTERVAL
    api_base_url = (api_base_url or os.getenv("GOPS_API_BASE_URL") or os.getenv("API_BASE_URL") or "http://localhost:8000").rstrip("/")
    redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kafka_bootstrap_servers = kafka_bootstrap_servers or os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    processor_group_id = processor_group_id or os.getenv("KAFKA_PROCESSOR_GROUP_ID") or os.getenv("KAFKA_FLINK_GROUP_ID") or "alfaka-stream-processor"
    raw_prefix = raw_prefix or os.getenv("KAFKA_RAW_TOPIC_PREFIX", os.getenv("KAFKA_TOPIC_PREFIX", "market.raw"))

    raw_topics = expected_raw_topics(raw_prefix)
    processed_topics = expected_processed_topics()
    checks = []
    checks.append(check_api(api_base_url, symbol, interval, timeout_seconds))
    checks.append(check_redis(redis_url, symbol, interval, require_live))
    checks.append(check_kafka(kafka_bootstrap_servers, processor_group_id, raw_topics, processed_topics))

    return {
        "checkedAt": utc_now_iso(),
        "symbol": symbol,
        "interval": interval,
        "status": overall_status(checks),
        "path": "Alpaca -> raw Kafka -> Python processor -> Redis/processed Kafka -> ClickHouse/API/WebSocket -> browser",
        "config": {
            "apiBaseUrl": api_base_url,
            "redisUrl": redact_url(redis_url),
            "kafkaBootstrapServers": kafka_bootstrap_servers,
            "processorGroupId": processor_group_id,
            "rawTopicPrefix": raw_prefix,
        },
        "checks": checks,
    }


def check_api(api_base_url, symbol, interval, timeout_seconds):
    try:
        health = requests.get(f"{api_base_url}/health", timeout=timeout_seconds)
        candles = requests.get(
            f"{api_base_url}/api/charts/candles",
            params={"symbol": symbol, "interval": interval, "limit": 20},
            timeout=timeout_seconds,
        )
        health_ok = health.ok
        candles_ok = candles.ok
        payload = candles.json() if candles_ok else {}
        status = "ok" if health_ok and candles_ok and payload.get("candles") else "warn"
        return trace_check(
            "api",
            status,
            healthStatus=health.status_code,
            candlesStatus=candles.status_code,
            dataStatus=payload.get("dataStatus"),
            returnedCount=payload.get("returnedCount"),
            sourceInterval=payload.get("sourceInterval"),
            newestTimestamp=payload.get("newestTimestamp"),
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
        processor_health_key = keys.component_health("market-processor")
        price = client.hgetall(price_key) if client.exists(price_key) else {}
        live = client.get(live_key)
        latest = client.get(latest_key)
        series_count = client.zcard(series_key)
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
