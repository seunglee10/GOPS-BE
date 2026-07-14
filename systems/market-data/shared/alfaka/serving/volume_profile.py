from __future__ import annotations

import math
from typing import Any, Literal


VOLUME_PROFILE_CALCULATION_VERSION = "volume-profile-v1"
VOLUME_PROFILE_EXACT_CALCULATION_VERSION = "volume-profile-exact-v2"
DEFAULT_VOLUME_PROFILE_TARGET_BINS = 10
DEFAULT_VALUE_AREA_PERCENT = 0.7
VolumeProfileBinningMode = Literal["adaptive", "exact"]


def compute_volume_profile_payload(
    raw_payload: dict[str, Any] | list[dict[str, Any]],
    *,
    symbol: str,
    interval: str = "1m",
    from_time: str,
    to_time: str,
    target_bins: int = DEFAULT_VOLUME_PROFILE_TARGET_BINS,
    price_min: float | None = None,
    price_max: float | None = None,
    binning_mode: VolumeProfileBinningMode = "adaptive",
    requested_candle_count: int | None = None,
) -> dict[str, Any]:
    source_rows = raw_payload.get("candles") if isinstance(raw_payload, dict) else raw_payload
    candles = normalize_source_candles(source_rows if isinstance(source_rows, list) else [])
    requested_target = normalize_target_bins(target_bins)
    calculation_version = calculation_version_for_binning_mode(binning_mode)
    source = first_string(raw_payload, candles, "source") or "candles"
    feed = first_string(raw_payload, candles, "feed") or "unknown"
    feed_profile = first_string(raw_payload, candles, "feedProfile")
    resolved_requested_candle_count = int(requested_candle_count) if requested_candle_count is not None else None
    data_is_partial = resolved_requested_candle_count is not None and len(candles) != resolved_requested_candle_count

    bucket_range = resolve_bucket_range(candles, price_min, price_max)
    if bucket_range is None:
        return {
            "symbol": symbol,
            "interval": interval,
            "sourceInterval": interval,
            "from": from_time,
            "to": to_time,
            "timeBucket": interval,
            "targetBins": requested_target,
            "bucketCount": 0,
            "priceBinSize": 0,
            "sourcePriceBinSize": None,
            "sourceBinCount": len(candles),
            "sourceCandleCount": len(candles),
            "requestedCandleCount": resolved_requested_candle_count,
            "source": source,
            "feed": feed,
            "feedProfile": feed_profile,
            "calculationVersion": calculation_version,
            "classificationVersion": calculation_version,
            "sideClassification": "estimated",
            "estimationMethod": "candle-range-volume-overlap",
            "dataStatus": "partial" if data_is_partial else "empty",
            "priceRange": {
                "min": price_min,
                "max": price_max,
                "requestedMin": price_min,
                "requestedMax": price_max,
            },
            "totalVolume": 0,
            "totalTradeCount": 0,
            "bins": [],
            "poc": None,
            "valueArea": None,
        }

    if binning_mode == "exact":
        domain_min, domain_max = bucket_range
        step = (domain_max - domain_min) / requested_target
        buckets = build_display_buckets(
            domain_min,
            domain_max,
            step,
            candles,
            exact_bucket_count=requested_target,
        )
    else:
        domain_min, domain_max, step = nice_display_domain(bucket_range[0], bucket_range[1], requested_target)
        buckets = build_display_buckets(domain_min, domain_max, step, candles)
    total_volume = sum(bucket["volume"] for bucket in buckets)
    total_trade_count = sum(bucket["tradeCount"] for bucket in buckets)
    poc = poc_payload(buckets)
    value_area = value_area_payload(buckets, total_volume)

    value_area_indexes = set(value_area["bucketIndexes"]) if value_area else set()
    for bucket in buckets:
        bucket["volumePercent"] = rounded(bucket["volume"] / total_volume) if total_volume > 0 else 0
        bucket["isPoc"] = bool(poc and bucket["index"] == poc["index"])
        bucket["inValueArea"] = bucket["index"] in value_area_indexes

    return {
        "symbol": symbol,
        "interval": interval,
        "sourceInterval": interval,
        "from": from_time,
        "to": to_time,
        "timeBucket": interval,
        "targetBins": requested_target,
        "bucketCount": len(buckets),
        "priceBinSize": rounded(step),
        "sourcePriceBinSize": None,
        "sourceBinCount": len(candles),
        "sourceCandleCount": len(candles),
        "requestedCandleCount": resolved_requested_candle_count,
        "source": source,
        "feed": feed,
        "feedProfile": feed_profile,
        "calculationVersion": calculation_version,
        "classificationVersion": calculation_version,
        "sideClassification": "estimated",
        "estimationMethod": "candle-range-volume-overlap",
        "dataStatus": "partial" if data_is_partial else ("ready" if total_volume > 0 else "empty"),
        "priceRange": {
            "min": rounded(domain_min),
            "max": rounded(domain_max),
            "requestedMin": price_min,
            "requestedMax": price_max,
        },
        "totalVolume": rounded(total_volume),
        "totalTradeCount": int(total_trade_count),
        "bins": buckets,
        "poc": poc,
        "valueArea": value_area,
    }


def normalize_source_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        low = number_or_none(row.get("low"))
        high = number_or_none(row.get("high"))
        close = number_or_none(row.get("close"))
        open_price = number_or_none(row.get("open"))
        fallback_price = first_present_number(close, open_price, low, high)
        if low is None:
            low = fallback_price
        if high is None:
            high = fallback_price
        if low is None or high is None:
            continue
        volume = number_or_none(row.get("volume")) or 0
        if volume < 0:
            continue
        if high < low:
            low, high = high, low
        typical = typical_price(high, low, close, fallback_price)
        normalized.append({
            **row,
            "priceLow": low,
            "priceHigh": high,
            "priceMid": typical,
            "volume": volume,
            "vwap": typical,
        })
    return normalized


def normalize_target_bins(value: int | float | str | None) -> int:
    try:
        parsed = int(value if value is not None else DEFAULT_VOLUME_PROFILE_TARGET_BINS)
    except (TypeError, ValueError):
        parsed = DEFAULT_VOLUME_PROFILE_TARGET_BINS
    return max(4, min(parsed, 48))


def calculation_version_for_binning_mode(binning_mode: VolumeProfileBinningMode) -> str:
    if binning_mode == "adaptive":
        return VOLUME_PROFILE_CALCULATION_VERSION
    if binning_mode == "exact":
        return VOLUME_PROFILE_EXACT_CALCULATION_VERSION
    raise ValueError(f"Unsupported volume profile binning mode: {binning_mode}")


def resolve_bucket_range(rows: list[dict[str, Any]], price_min: float | None, price_max: float | None) -> tuple[float, float] | None:
    if not rows:
        return None
    requested_min = number_or_none(price_min)
    requested_max = number_or_none(price_max)
    if requested_min is not None and requested_max is not None:
        if requested_max <= requested_min:
            return None
        return requested_min, requested_max
    min_price = requested_min if requested_min is not None else min(row["priceLow"] for row in rows)
    max_price = requested_max if requested_max is not None else max(row["priceHigh"] for row in rows)
    if max_price <= min_price:
        max_price = min_price + max(0.01, abs(min_price) * 0.001)
    return min_price, max_price


def nice_display_domain(price_min: float, price_max: float, target_bins: int) -> tuple[float, float, float]:
    raw_range = max(0.01, price_max - price_min)
    step = nice_price_step(raw_range / max(1, target_bins))
    domain_min = math.floor(price_min / step) * step
    domain_max = math.ceil(price_max / step) * step
    bucket_count = bucket_count_for_domain(domain_min, domain_max, step)
    while bucket_count > max(6, target_bins * 2):
        step = nice_price_step(step * 1.6)
        domain_min = math.floor(price_min / step) * step
        domain_max = math.ceil(price_max / step) * step
        bucket_count = bucket_count_for_domain(domain_min, domain_max, step)
    while bucket_count < max(3, target_bins // 2):
        smaller = nice_price_step(step / 2.5)
        if smaller <= 0 or smaller == step:
            break
        step = smaller
        domain_min = math.floor(price_min / step) * step
        domain_max = math.ceil(price_max / step) * step
        bucket_count = bucket_count_for_domain(domain_min, domain_max, step)
    return domain_min, domain_max, step


def build_display_buckets(
    domain_min: float,
    domain_max: float,
    step: float,
    rows: list[dict[str, Any]],
    *,
    exact_bucket_count: int | None = None,
) -> list[dict[str, Any]]:
    count = exact_bucket_count if exact_bucket_count is not None else bucket_count_for_domain(domain_min, domain_max, step)
    buckets = []
    allocation_bounds: list[tuple[float, float]] = []
    weighted_vwap = [0.0 for _ in range(count)]
    for index in range(count):
        lower = domain_min + index * step
        upper = domain_max if exact_bucket_count is not None and index == count - 1 else lower + step
        display_lower = rounded(lower)
        display_upper = rounded(upper)
        allocation_bounds.append(
            (lower, upper) if exact_bucket_count is not None else (display_lower, display_upper)
        )
        buckets.append({
            "index": index,
            "priceBin": display_lower,
            "priceBinSize": rounded(step),
            "priceMin": display_lower,
            "priceMax": display_upper,
            "priceMid": rounded((lower + upper) / 2),
            "volume": 0.0,
            "tradeCount": 0,
            "vwap": None,
            "volumePercent": 0,
            "isPoc": False,
            "inValueArea": False,
            "sourceCandleCount": 0,
        })
    for candle in rows:
        volume = candle["volume"]
        if volume <= 0:
            continue
        allocations = candle_bucket_allocations(
            candle,
            buckets,
            domain_min,
            domain_max,
            step,
            allocation_bounds=allocation_bounds,
        )
        for index, allocated_volume in allocations:
            if allocated_volume <= 0:
                continue
            bucket = buckets[index]
            bucket["volume"] += allocated_volume
            bucket["sourceCandleCount"] += 1
            weighted_vwap[index] += candle["priceMid"] * allocated_volume
    for index, bucket in enumerate(buckets):
        volume = bucket["volume"]
        bucket["volume"] = rounded(volume)
        bucket["tradeCount"] = int(bucket["tradeCount"])
        bucket["vwap"] = rounded(weighted_vwap[index] / volume) if volume > 0 and weighted_vwap[index] > 0 else None
    return buckets


def candle_bucket_allocations(
    candle: dict[str, Any],
    buckets: list[dict[str, Any]],
    domain_min: float,
    domain_max: float,
    step: float,
    *,
    allocation_bounds: list[tuple[float, float]] | None = None,
) -> list[tuple[int, float]]:
    low = candle["priceLow"]
    high = candle["priceHigh"]
    volume = candle["volume"]
    if high > low:
        overlaps: list[tuple[int, float]] = []
        total_overlap = 0.0
        for index, bucket in enumerate(buckets):
            bucket_min, bucket_max = (
                allocation_bounds[index]
                if allocation_bounds is not None
                else (bucket["priceMin"], bucket["priceMax"])
            )
            overlap = max(0.0, min(high, bucket_max) - max(low, bucket_min))
            if overlap > 0:
                overlaps.append((index, overlap))
                total_overlap += overlap
        if total_overlap > 0:
            return [(index, volume * (overlap / total_overlap)) for index, overlap in overlaps]
    index = bucket_index_for_price(candle["priceMid"], domain_min, domain_max, step, len(buckets))
    return [(index, volume)] if index is not None else []


def bucket_index_for_price(price: float | None, domain_min: float, domain_max: float, step: float, count: int) -> int | None:
    if price is None or not math.isfinite(price) or price < domain_min or price > domain_max:
        return None
    if price == domain_max:
        return count - 1
    return min(count - 1, max(0, int((price - domain_min) / step)))


def poc_payload(buckets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not buckets:
        return None
    poc = max(buckets, key=lambda bucket: bucket["volume"])
    if poc["volume"] <= 0:
        return None
    return bucket_summary(poc)


def value_area_payload(buckets: list[dict[str, Any]], total_volume: float, target_percent: float = DEFAULT_VALUE_AREA_PERCENT) -> dict[str, Any] | None:
    poc = poc_payload(buckets)
    if poc is None or total_volume <= 0:
        return None
    included = {poc["index"]}
    included_volume = buckets[poc["index"]]["volume"]
    left = poc["index"] - 1
    right = poc["index"] + 1
    target_volume = total_volume * target_percent
    while included_volume < target_volume and (left >= 0 or right < len(buckets)):
        left_volume = buckets[left]["volume"] if left >= 0 else -1
        right_volume = buckets[right]["volume"] if right < len(buckets) else -1
        if right_volume > left_volume:
            included.add(right)
            included_volume += max(0, right_volume)
            right += 1
        else:
            included.add(left)
            included_volume += max(0, left_volume)
            left -= 1
    indexes = sorted(included)
    return {
        "low": buckets[indexes[0]]["priceMin"],
        "high": buckets[indexes[-1]]["priceMax"],
        "volume": rounded(included_volume),
        "volumePercent": rounded(included_volume / total_volume),
        "targetPercent": target_percent,
        "bucketIndexes": indexes,
    }


def bucket_summary(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": bucket["index"],
        "priceMin": bucket["priceMin"],
        "priceMax": bucket["priceMax"],
        "priceMid": bucket["priceMid"],
        "volume": bucket["volume"],
        "tradeCount": bucket["tradeCount"],
    }


def bucket_count_for_domain(domain_min: float, domain_max: float, step: float) -> int:
    return max(1, min(96, int(math.ceil((domain_max - domain_min) / step))))


def nice_price_step(raw_step: float) -> float:
    if not math.isfinite(raw_step) or raw_step <= 0:
        return 0.01
    exponent = math.floor(math.log10(raw_step))
    base = 10 ** exponent
    fraction = raw_step / base
    for multiple in (1, 2, 2.5, 5, 10):
        if fraction <= multiple:
            return multiple * base
    return 10 * base


def first_string(raw_payload: dict[str, Any] | list[dict[str, Any]], rows: list[dict[str, Any]], key: str) -> str | None:
    if isinstance(raw_payload, dict):
        value = raw_payload.get(key)
        if isinstance(value, str) and value:
            return value
    for row in rows:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def first_present_number(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def typical_price(high: float, low: float, close: float | None, fallback: float | None) -> float:
    if close is not None:
        return (high + low + close) / 3
    if fallback is not None:
        return fallback
    return (high + low) / 2


def number_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 8)
