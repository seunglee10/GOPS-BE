from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from alfaka.serving.indicators import INDICATOR_CALCULATION_VERSION, IndicatorSpec
from alfaka.serving.intervals import normalize_chart_interval
from alfaka.serving.time_utils import parse_utc_time
from alfaka.serving.volume_profile import VOLUME_PROFILE_CALCULATION_VERSION


DERIVED_KIND_INDICATORS = "indicators"
DERIVED_KIND_VOLUME_PROFILE = "volumeProfile"

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


def lock_key(request_hash: str) -> str:
    return f"chart:derived:lock:{request_hash}"


def redis_ttl_seconds(kind: str) -> int:
    if kind == DERIVED_KIND_INDICATORS:
        return int_env("CHART_INDICATOR_CACHE_TTL_SECONDS", 300)
    if kind == DERIVED_KIND_VOLUME_PROFILE:
        return int_env("CHART_VOLUME_PROFILE_CACHE_TTL_SECONDS", 30)
    return 60


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


def with_derived_metadata(
    payload: dict[str, Any],
    request: dict[str, Any],
    *,
    state: str,
    source: str,
    error: str | None = None,
) -> dict[str, Any]:
    if state not in {"ready", "failed"}:
        raise ValueError(f"Unsupported derived response state: {state}")
    if source not in {"api-compute", "redis"}:
        raise ValueError(f"Unsupported derived response source: {source}")
    next_payload = dict(payload)
    metadata = {
        "state": state,
        "source": source,
        "requestHash": request["requestHash"],
        "generatedAt": utc_now_iso(),
    }
    if error:
        metadata["error"] = error
    next_payload["derived"] = metadata
    return next_payload


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


def utc_now_iso() -> str:
    return iso_time(datetime.now(timezone.utc))


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"
