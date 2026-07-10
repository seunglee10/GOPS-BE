from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from alfaka.serving.indicators import INDICATOR_CALCULATION_VERSION, IndicatorSpec
from alfaka.serving.intervals import normalize_chart_interval
from alfaka.serving.time_utils import parse_utc_time
from alfaka.serving.volume_profile import VOLUME_PROFILE_CALCULATION_VERSION


DERIVED_KIND_INDICATORS = "indicators"
DERIVED_KIND_VOLUME_PROFILE = "volumeProfile"

DERIVED_REQUEST_TOPIC = "market.chart-derived.requests.v1"
DERIVED_DLQ_TOPIC = "market.chart-derived.dlq.v1"


def build_indicator_request(
    *,
    symbol: str,
    interval: str,
    from_time: str | None,
    to_time: str | None,
    specs: list[IndicatorSpec],
    limit: int,
) -> dict[str, Any]:
    layer_ids = ",".join(spec.id for spec in specs)
    identity = {
        "version": INDICATOR_CALCULATION_VERSION,
        "symbol": symbol,
        "interval": interval,
        "from": from_time,
        "to": to_time,
        "limit": int(limit),
        "layers": layer_ids,
    }
    return build_request(
        DERIVED_KIND_INDICATORS,
        symbol=symbol,
        interval=interval,
        from_time=from_time,
        to_time=to_time,
        limit=int(limit),
        parameters={"layers": layer_ids},
        calculation_version=INDICATOR_CALCULATION_VERSION,
        identity=identity,
        cache_key=indicator_cache_key_from_identity(symbol, interval, identity),
    )


def build_volume_profile_request(
    *,
    symbol: str,
    interval: str = "1m",
    from_time: str,
    to_time: str,
    price_bin_size: str,
    target_bins: int,
    price_min: float | None,
    price_max: float | None,
) -> dict[str, Any]:
    interval = normalize_chart_interval(interval)
    identity = {
        "version": VOLUME_PROFILE_CALCULATION_VERSION,
        "symbol": symbol,
        "interval": interval,
        "from": from_time,
        "to": to_time,
        "priceBinSize": price_bin_size,
        "targetBins": int(target_bins),
        "priceMin": price_min,
        "priceMax": price_max,
    }
    return build_request(
        DERIVED_KIND_VOLUME_PROFILE,
        symbol=symbol,
        interval=interval,
        from_time=from_time,
        to_time=to_time,
        limit=None,
        parameters={
            "priceBinSize": price_bin_size,
            "targetBins": int(target_bins),
            "priceMin": price_min,
            "priceMax": price_max,
        },
        calculation_version=VOLUME_PROFILE_CALCULATION_VERSION,
        identity=identity,
        cache_key=volume_profile_cache_key_from_identity(symbol, identity),
    )


def build_request(
    kind: str,
    *,
    symbol: str,
    interval: str,
    from_time: str | None,
    to_time: str | None,
    limit: int | None,
    parameters: dict[str, Any],
    calculation_version: str,
    identity: dict[str, Any],
    cache_key: str,
) -> dict[str, Any]:
    request_hash = request_hash_for(kind, identity)
    return {
        "requestId": f"chart-derived:{request_hash}",
        "requestHash": request_hash,
        "kind": kind,
        "symbol": symbol,
        "interval": interval,
        "from": from_time,
        "to": to_time,
        "limit": limit,
        "parameters": parameters,
        "calculationVersion": calculation_version,
        "cacheKey": cache_key,
        "statusKey": status_key(request_hash),
        "lockKey": lock_key(request_hash),
        "requestedAt": utc_now_iso(),
    }


def request_hash_for(kind: str, identity: dict[str, Any]) -> str:
    payload = {"kind": kind, **identity}
    return digest_json(payload)


def indicator_cache_key_from_identity(symbol: str, interval: str, identity: dict[str, Any]) -> str:
    return f"chart:indicators:{INDICATOR_CALCULATION_VERSION}:{symbol}:{interval}:{digest_json(identity)}"


def volume_profile_cache_key_from_identity(symbol: str, identity: dict[str, Any]) -> str:
    return f"chart:volume-profile:{VOLUME_PROFILE_CALCULATION_VERSION}:{symbol}:{digest_json(identity)}"


def digest_json(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def status_key(request_hash: str) -> str:
    return f"chart:derived:status:{request_hash}"


def lock_key(request_hash: str) -> str:
    return f"chart:derived:lock:{request_hash}"


def redis_ttl_seconds(kind: str) -> int:
    if kind == DERIVED_KIND_INDICATORS:
        return int_env("CHART_INDICATOR_CACHE_TTL_SECONDS", 300)
    if kind == DERIVED_KIND_VOLUME_PROFILE:
        return int_env("CHART_VOLUME_PROFILE_CACHE_TTL_SECONDS", 30)
    return 60


def artifact_retention_seconds(kind: str) -> int:
    if kind == DERIVED_KIND_INDICATORS:
        return int_env("CHART_INDICATOR_ARTIFACT_RETENTION_SECONDS", 604800)
    if kind == DERIVED_KIND_VOLUME_PROFILE:
        return int_env("CHART_VOLUME_PROFILE_ARTIFACT_RETENTION_SECONDS", 86400)
    return 86400


def api_wait_ms() -> int:
    return int_env("CHART_DERIVED_API_WAIT_MS", 1200)


def api_poll_ms() -> int:
    return max(25, int_env("CHART_DERIVED_API_POLL_MS", 100))


def retry_after_ms() -> int:
    return int_env("CHART_DERIVED_RETRY_AFTER_MS", 1000)


def int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def read_json_cache(redis_client: Any, key: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        value = redis_client.get(key)
    except Exception:
        return None
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def write_json_cache(redis_client: Any, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
    if redis_client is None or ttl_seconds <= 0:
        return
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        if hasattr(redis_client, "setex"):
            redis_client.setex(key, int(ttl_seconds), encoded)
            return
        redis_client.set(key, encoded)
        if hasattr(redis_client, "expire"):
            redis_client.expire(key, int(ttl_seconds))
    except Exception:
        return


def write_status(redis_client: Any, request: dict[str, Any], state: str, *, error: str | None = None) -> None:
    payload = {
        "state": state,
        "requestHash": request["requestHash"],
        "kind": request["kind"],
        "updatedAt": utc_now_iso(),
        **({"error": error} if error else {}),
    }
    write_json_cache(redis_client, request["statusKey"], payload, max(60, redis_ttl_seconds(request["kind"]) * 2))


def read_status(redis_client: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    return read_json_cache(redis_client, request["statusKey"])


def acquire_enqueue_lock(redis_client: Any, request: dict[str, Any]) -> bool:
    if redis_client is None:
        return True
    ttl = max(5, int_env("CHART_DERIVED_ENQUEUE_LOCK_TTL_SECONDS", 30))
    try:
        return bool(redis_client.set(request["lockKey"], "1", ex=ttl, nx=True))
    except TypeError:
        try:
            existing = redis_client.get(request["lockKey"])
            if existing:
                return False
            redis_client.setex(request["lockKey"], ttl, "1")
            return True
        except Exception:
            return True
    except Exception:
        return True


class ChartDerivedArtifactStore:
    def __init__(self, clickhouse_client: Any):
        self.client = clickhouse_client

    def read(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if self.client is None:
            return None
        query = f"""
        SELECT
          payload_json AS payloadJson
        FROM {self.client.database}.chart_derived_artifacts
        WHERE request_hash = {{requestHash:String}}
          AND expires_at > now64(3)
        ORDER BY inserted_at DESC
        LIMIT 1
        FORMAT JSONEachRow
        """
        try:
            rows = self.client.query_json_each_row(query, {"requestHash": request["requestHash"]})
        except Exception:
            return None
        if not rows:
            return None
        payload_json = rows[0].get("payloadJson")
        if not isinstance(payload_json, str) or not payload_json:
            return None
        try:
            payload = json.loads(payload_json)
        except ValueError:
            return None
        if not should_store_derived_artifact(request, payload):
            return None
        return with_derived_metadata(payload, request, state="ready", source="clickhouse", artifact_stored=True)

    def write(self, request: dict[str, Any], payload: dict[str, Any]) -> None:
        if self.client is None:
            return
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=max(1, artifact_retention_seconds(request["kind"])))
        row = {
            "request_hash": request["requestHash"],
            "kind": request["kind"],
            "symbol": request["symbol"],
            "interval": request.get("interval") or "",
            "from_time": clickhouse_time_or_none(request.get("from")),
            "to_time": clickhouse_time_or_none(request.get("to")),
            "parameters_json": json.dumps(request.get("parameters") or {}, ensure_ascii=False, separators=(",", ":")),
            "calculation_version": request["calculationVersion"],
            "data_status": payload.get("dataStatus") or payload.get("status") or "unknown",
            "payload_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "source": payload.get("source") or "chart-derived-data-worker",
            "feed": payload.get("feed") or "unknown",
            "created_at": clickhouse_json_time(now),
            "expires_at": clickhouse_json_time(expires_at),
        }
        self.client.insert_json_each_row("chart_derived_artifacts", [row])


class ChartDerivedDataClient:
    def __init__(
        self,
        *,
        redis_client: Any = None,
        artifact_store: ChartDerivedArtifactStore | None = None,
        producer: Any = None,
        wait_ms: int | None = None,
        poll_ms: int | None = None,
    ):
        self.redis_client = redis_client
        self.artifact_store = artifact_store
        self.producer = producer
        self.wait_ms = api_wait_ms() if wait_ms is None else wait_ms
        self.poll_ms = api_poll_ms() if poll_ms is None else poll_ms

    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        cached = read_json_cache(self.redis_client, request["cacheKey"])
        if cached:
            return with_derived_metadata(cached, request, state="ready", source="redis", artifact_stored=bool(cached.get("derived", {}).get("artifactStored")))

        artifact = self.artifact_store.read(request) if self.artifact_store else None
        if artifact:
            write_json_cache(self.redis_client, request["cacheKey"], artifact, redis_ttl_seconds(request["kind"]))
            return artifact

        try:
            if acquire_enqueue_lock(self.redis_client, request):
                write_status(self.redis_client, request, "queued")
                enqueue_derived_request(request, self.producer)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            write_status(self.redis_client, request, "failed", error=error)
            return pending_payload(request, state="failed", error=error)

        waited = wait_for_result(self.redis_client, request, self.wait_ms, self.poll_ms)
        if waited:
            return with_derived_metadata(waited, request, state="ready", source="worker", artifact_stored=bool(waited.get("derived", {}).get("artifactStored")))

        status = read_status(self.redis_client, request)
        if isinstance(status, dict) and status.get("state") == "failed":
            return pending_payload(request, state="failed", error=str(status.get("error") or "Derived worker failed."))
        return pending_payload(request)


def with_derived_metadata(
    payload: dict[str, Any],
    request: dict[str, Any],
    *,
    state: str,
    source: str,
    artifact_stored: bool = False,
    retry_after_ms_value: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    next_payload = dict(payload)
    metadata = {
        "state": state,
        "source": source,
        "requestHash": request["requestHash"],
        "artifactStored": bool(artifact_stored),
        "generatedAt": utc_now_iso(),
    }
    if retry_after_ms_value is not None:
        metadata["retryAfterMs"] = retry_after_ms_value
    if error:
        metadata["error"] = error
    next_payload["derived"] = {**next_payload.get("derived", {}), **metadata}
    return next_payload


def pending_payload(request: dict[str, Any], *, error: str | None = None, state: str = "pending") -> dict[str, Any]:
    kind = request["kind"]
    if kind == DERIVED_KIND_INDICATORS:
        payload = {
            "symbol": request["symbol"],
            "interval": request["interval"],
            "from": request.get("from"),
            "to": request.get("to"),
            "requestedLimit": int(request.get("limit") or 0),
            "lookbackBars": 0,
            "returnedCandleCount": 0,
            "source": "worker",
            "feed": "unknown",
            "dataStatus": state,
            "calculationVersion": request["calculationVersion"],
            "indicators": [],
            "series": {},
            "cache": {"hit": False, "ttlSeconds": redis_ttl_seconds(kind), "keyVersion": request["calculationVersion"]},
        }
    elif kind == DERIVED_KIND_VOLUME_PROFILE:
        params = request.get("parameters") or {}
        payload = {
            "symbol": request["symbol"],
            "interval": request.get("interval") or "1m",
            "sourceInterval": request.get("interval") or "1m",
            "from": request.get("from"),
            "to": request.get("to"),
            "timeBucket": request.get("interval") or "1m",
            "targetBins": int(params.get("targetBins") or 10),
            "bucketCount": 0,
            "priceBinSize": 0,
            "sourcePriceBinSize": None,
            "sourceBinCount": 0,
            "sourceCandleCount": 0,
            "source": "worker",
            "feed": "unknown",
            "calculationVersion": request["calculationVersion"],
            "classificationVersion": request["calculationVersion"],
            "sideClassification": "estimated",
            "estimationMethod": "candle-range-volume-overlap",
            "dataStatus": state,
            "priceRange": {"min": None, "max": None, "requestedMin": params.get("priceMin"), "requestedMax": params.get("priceMax")},
            "totalVolume": 0,
            "totalTradeCount": 0,
            "bins": [],
            "poc": None,
            "valueArea": None,
            "cache": {"hit": False, "ttlSeconds": redis_ttl_seconds(kind), "keyVersion": request["calculationVersion"]},
        }
    else:
        raise ValueError(f"Unsupported chart derived kind: {kind}")
    return with_derived_metadata(
        payload,
        request,
        state=state,
        source="queued" if state == "pending" else "worker",
        retry_after_ms_value=retry_after_ms() if state == "pending" else None,
        error=error,
    )


def enqueue_derived_request(request: dict[str, Any], producer: Any | None = None) -> None:
    producer = producer or kafka_producer("gops-chart-derived-api")
    topic = os.getenv("CHART_DERIVED_REQUEST_TOPIC", DERIVED_REQUEST_TOPIC)
    future = producer.send(topic, key=request["requestHash"], value=request)
    if hasattr(future, "get"):
        future.get(timeout=float(os.getenv("CHART_DERIVED_PRODUCE_TIMEOUT_SECONDS", "1.5")))
    elif hasattr(producer, "flush"):
        producer.flush(timeout=float(os.getenv("CHART_DERIVED_PRODUCE_TIMEOUT_SECONDS", "1.5")))


def should_store_derived_artifact(request: dict[str, Any], payload: dict[str, Any]) -> bool:
    return bool(request and payload)


@lru_cache(maxsize=8)
def kafka_producer(client_id: str):
    from alfaka.common.kafka_io import create_json_producer

    return create_json_producer(os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"), client_id)


def wait_for_result(redis_client: Any, request: dict[str, Any], wait_ms: int | None = None, poll_ms: int | None = None) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    deadline = time.monotonic() + ((api_wait_ms() if wait_ms is None else wait_ms) / 1000)
    interval = (api_poll_ms() if poll_ms is None else max(25, poll_ms)) / 1000
    while time.monotonic() < deadline:
        cached = read_json_cache(redis_client, request["cacheKey"])
        if cached:
            return cached
        time.sleep(interval)
    return read_json_cache(redis_client, request["cacheKey"])


def indicator_fetch_from_time(interval: str, from_time: str | None, lookback_bars: int) -> str | None:
    parsed = parse_utc_time(from_time)
    if not parsed or lookback_bars <= 0:
        return from_time
    return (parsed - indicator_lookback_delta(interval, lookback_bars)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def indicator_lookback_delta(interval: str, lookback_bars: int) -> timedelta:
    bars = max(1, int(lookback_bars))
    interval = normalize_chart_interval(interval)
    if interval == "5m":
        return timedelta(minutes=bars * 5 * 2)
    if interval == "10m":
        return timedelta(minutes=bars * 10 * 2)
    if interval == "1D":
        return timedelta(days=bars * 2)
    if interval == "1W":
        return timedelta(days=bars * 8)
    if interval == "1M":
        return timedelta(days=bars * 32)
    return timedelta(minutes=bars * 2)


def clickhouse_client_from_env():
    from alfaka.storage.clickhouse_loader import ClickHouseHttpClient

    return ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )


def utc_now_iso() -> str:
    return iso_time(datetime.now(timezone.utc))


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"


def clickhouse_json_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


def clickhouse_time_or_none(value: Any) -> str | None:
    if not value:
        return None
    parsed = parse_utc_time(value)
    return clickhouse_json_time(parsed) if parsed else None
