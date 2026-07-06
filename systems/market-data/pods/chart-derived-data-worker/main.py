# 역할: 차트 렌더링용 derived data(indicators, volume profile, footprint)를 계산해 Redis와 ClickHouse artifact에 저장합니다.
# 사용: Kafka market.chart-derived.requests.v1 요청을 소비하는 장기 실행 worker입니다.
from __future__ import annotations

import json
import os
import sys
from typing import Any

import redis

from alfaka.common.env import load_dotenv
from alfaka.common.kafka_io import create_json_consumer, create_json_producer
from alfaka.serving.chart_derived_data import (
    DERIVED_DLQ_TOPIC,
    DERIVED_KIND_FOOTPRINT,
    DERIVED_KIND_INDICATORS,
    DERIVED_KIND_VOLUME_PROFILE,
    DERIVED_REQUEST_TOPIC,
    ChartDerivedArtifactStore,
    artifact_retention_seconds,
    clickhouse_client_from_env,
    indicator_fetch_from_time,
    redis_ttl_seconds,
    write_json_cache,
    write_status,
    with_derived_metadata,
)
from alfaka.serving.footprint import compute_footprint_payload
from alfaka.serving.indicators import (
    compute_indicator_payload,
    indicator_required_lookback_bars,
    indicator_specs_from_csv,
)
from alfaka.serving.intervals import normalize_chart_interval, resolve_candle_limit
from alfaka.serving.provider import MarketDataProvider
from alfaka.serving.volume_profile import compute_volume_profile_payload, normalize_target_bins
from alfaka.storage.clickhouse_loader import should_ensure_schema_on_start


def main() -> None:
    load_dotenv()
    if os.getenv("CHART_DERIVED_WORKER_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        print("Chart derived data worker disabled: CHART_DERIVED_WORKER_ENABLED=false", flush=True)
        return

    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    request_topic = os.getenv("CHART_DERIVED_REQUEST_TOPIC", DERIVED_REQUEST_TOPIC)
    group_id = os.getenv("CHART_DERIVED_WORKER_GROUP_ID", "alfaka-chart-derived-data-worker")
    enable_auto_commit = os.getenv("CHART_DERIVED_ENABLE_AUTO_COMMIT", "false").strip().lower() in {"1", "true", "yes", "on"}
    consumer = create_json_consumer(
        [request_topic],
        kafka_servers,
        group_id,
        "alfaka-chart-derived-data-worker",
        enable_auto_commit=enable_auto_commit,
        max_poll_interval_ms=int(os.getenv("CHART_DERIVED_MAX_POLL_INTERVAL_MS", "900000")),
        max_poll_records=int(os.getenv("CHART_DERIVED_MAX_POLL_RECORDS", "5")),
    )
    producer = create_json_producer(kafka_servers, "alfaka-chart-derived-data-worker")
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    clickhouse = clickhouse_client_from_env()
    if should_ensure_schema_on_start():
        clickhouse.ensure_market_data_schema()
    artifact_store = ChartDerivedArtifactStore(clickhouse)
    provider = MarketDataProvider()
    print(f"Chart derived data worker 시작: topic={request_topic} group={group_id}", flush=True)

    for record in consumer:
        try:
            process_request(
                record.value,
                provider=provider,
                redis_client=redis_client,
                artifact_store=artifact_store,
                dlq_producer=producer,
            )
            if not enable_auto_commit:
                consumer.commit()
        except Exception as exc:
            print(f"Chart derived 처리 실패: {exc}; payload={json.dumps(record.value, ensure_ascii=False)}", file=sys.stderr, flush=True)


def process_request(
    request: dict[str, Any],
    *,
    provider: Any,
    redis_client: Any,
    artifact_store: ChartDerivedArtifactStore,
    dlq_producer: Any | None = None,
) -> dict[str, Any] | None:
    if not isinstance(request, dict) or not request.get("requestHash") or not request.get("kind"):
        return None
    try:
        write_status(redis_client, request, "running")
        payload = compute_request_payload(request, provider=provider)
        payload = with_derived_metadata(payload, request, state="ready", source="worker", artifact_stored=True)
        artifact_store.write(request, payload)
        write_json_cache(redis_client, request["cacheKey"], payload, redis_ttl_seconds(request["kind"]))
        write_status(redis_client, request, "ready")
        return payload
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        write_status(redis_client, request, "failed", error=error)
        publish_dlq(request, error, dlq_producer)
        return None


def compute_request_payload(request: dict[str, Any], *, provider: Any) -> dict[str, Any]:
    kind = request["kind"]
    if kind == DERIVED_KIND_INDICATORS:
        return compute_indicator_request(request, provider=provider)
    if kind == DERIVED_KIND_VOLUME_PROFILE:
        return compute_volume_profile_request(request, provider=provider)
    if kind == DERIVED_KIND_FOOTPRINT:
        return compute_footprint_request(request, provider=provider)
    raise ValueError(f"Unsupported chart derived kind: {kind}")


def compute_indicator_request(request: dict[str, Any], *, provider: Any) -> dict[str, Any]:
    symbol = str(request["symbol"]).upper()
    interval = normalize_chart_interval(str(request.get("interval") or "1m"))
    from_time = request.get("from")
    to_time = request.get("to")
    requested_limit = resolve_candle_limit(interval, request.get("limit"))
    specs = indicator_specs_from_csv((request.get("parameters") or {}).get("layers"))
    lookback = indicator_required_lookback_bars(specs)
    fetch_limit = requested_limit + lookback
    if from_time and lookback > 0:
        warmup_payload = provider_candle_snapshot(
            provider,
            symbol,
            interval,
            lookback,
            before=from_time,
            from_time=None,
            to_time=None,
        )
        range_payload = provider_candle_snapshot(
            provider,
            symbol,
            interval,
            requested_limit,
            before=None,
            from_time=from_time,
            to_time=to_time,
        )
        candle_payload = merge_candle_payloads(warmup_payload, range_payload)
    else:
        fetch_from_time = indicator_fetch_from_time(interval, from_time, lookback)
        candle_payload = provider_candle_snapshot(
            provider,
            symbol,
            interval,
            fetch_limit,
            before=None,
            from_time=fetch_from_time,
            to_time=to_time,
        )
    computed = compute_indicator_payload(
        candle_payload.get("candles") or [],
        specs,
        from_time=from_time,
        to_time=to_time,
    )
    return {
        "symbol": symbol,
        "interval": interval,
        "from": from_time,
        "to": to_time,
        "requestedLimit": requested_limit,
        "lookbackBars": lookback,
        "returnedCandleCount": len(candle_payload.get("candles") or []),
        "source": candle_payload.get("source", "alpaca"),
        "feed": candle_payload.get("feed", "unknown"),
        "dataStatus": candle_payload.get("dataStatus", "empty"),
        "cache": {"hit": False, "ttlSeconds": redis_ttl_seconds(DERIVED_KIND_INDICATORS), "keyVersion": request["calculationVersion"]},
        **computed,
    }


def compute_volume_profile_request(request: dict[str, Any], *, provider: Any) -> dict[str, Any]:
    params = request.get("parameters") or {}
    symbol = str(request["symbol"]).upper()
    interval = normalize_chart_interval(str(request.get("interval") or "1m"))
    from_time = str(request.get("from") or "")
    to_time = str(request.get("to") or "")
    target_bins = normalize_target_bins(params.get("targetBins"))
    price_min = number_or_none(params.get("priceMin"))
    price_max = number_or_none(params.get("priceMax"))
    candle_payload = provider_candle_snapshot(
        provider,
        symbol,
        interval,
        resolve_candle_limit(interval, request.get("limit")),
        before=None,
        from_time=from_time,
        to_time=to_time,
    )
    result = compute_volume_profile_payload(
        candle_payload,
        symbol=symbol,
        interval=interval,
        from_time=from_time,
        to_time=to_time,
        target_bins=target_bins,
        price_min=price_min,
        price_max=price_max,
    )
    result["cache"] = {"hit": False, "ttlSeconds": redis_ttl_seconds(DERIVED_KIND_VOLUME_PROFILE), "keyVersion": request["calculationVersion"]}
    return result


def compute_footprint_request(request: dict[str, Any], *, provider: Any) -> dict[str, Any]:
    method = getattr(provider, "footprint_ticks", None)
    if not callable(method):
        raise RuntimeError("Footprint provider is unavailable.")
    symbol = str(request["symbol"]).upper()
    from_time = str(request.get("from") or "")
    to_time = str(request.get("to") or "")
    resolved_limit = max(1, min(int(request.get("limit") or 20000), 100000))
    raw_payload = method(symbol, from_time, to_time, limit=resolved_limit)
    result = compute_footprint_payload(raw_payload, symbol=symbol, from_time=from_time, to_time=to_time)
    result["requestedLimit"] = resolved_limit
    result["cache"] = {"hit": False, "ttlSeconds": redis_ttl_seconds(DERIVED_KIND_FOOTPRINT), "keyVersion": request["calculationVersion"]}
    return result


def merge_candle_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    by_timestamp: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not merged:
            merged = dict(payload)
        else:
            merged.update({key: value for key, value in payload.items() if key != "candles"})
        for candle in payload.get("candles") or []:
            timestamp = str(candle.get("timestamp") or "")
            if timestamp:
                by_timestamp[timestamp] = candle
    merged["candles"] = [by_timestamp[key] for key in sorted(by_timestamp)]
    return merged


def provider_candle_snapshot(
    provider: Any,
    symbol: str,
    interval: str,
    limit: int,
    *,
    before: str | None,
    from_time: str | None,
    to_time: str | None,
) -> dict[str, Any]:
    try:
        return provider.candle_snapshot(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time, ma_windows=())
    except TypeError:
        return provider.candle_snapshot(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)


def publish_dlq(request: dict[str, Any], error: str, producer: Any | None) -> None:
    if producer is None:
        return
    topic = os.getenv("CHART_DERIVED_DLQ_TOPIC", DERIVED_DLQ_TOPIC)
    payload = {
        "eventType": "CHART_DERIVED_FAILED",
        "request": request,
        "error": error,
        "retentionSeconds": artifact_retention_seconds(str(request.get("kind") or "")),
    }
    try:
        future = producer.send(topic, key=str(request.get("requestHash") or "unknown"), value=payload)
        timeout = float(os.getenv("CHART_DERIVED_PRODUCE_TIMEOUT_SECONDS", "1.5"))
        if hasattr(future, "get"):
            future.get(timeout=timeout)
        elif hasattr(producer, "flush"):
            producer.flush(timeout=timeout)
    except Exception:
        return


def number_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
