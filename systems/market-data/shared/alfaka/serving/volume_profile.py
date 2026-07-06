from __future__ import annotations

import math
from typing import Any


VOLUME_PROFILE_CALCULATION_VERSION = "volume-profile-v1"
DEFAULT_VOLUME_PROFILE_TARGET_BINS = 10
DEFAULT_VALUE_AREA_PERCENT = 0.7


def compute_volume_profile_payload(
    raw_payload: dict[str, Any] | list[dict[str, Any]],
    *,
    symbol: str,
    from_time: str,
    to_time: str,
    target_bins: int = DEFAULT_VOLUME_PROFILE_TARGET_BINS,
    price_min: float | None = None,
    price_max: float | None = None,
) -> dict[str, Any]:
    source_bins = raw_payload.get("bins") if isinstance(raw_payload, dict) else raw_payload
    rows = normalize_source_bins(source_bins if isinstance(source_bins, list) else [])
    requested_target = normalize_target_bins(target_bins)
    source_price_bin_size = first_number(rows, "priceBinSize")
    source = first_string(raw_payload, rows, "source") or "clickhouse"
    feed = first_string(raw_payload, rows, "feed") or "unknown"
    feed_profile = first_string(raw_payload, rows, "feedProfile")

    bucket_range = resolve_bucket_range(rows, price_min, price_max)
    if bucket_range is None:
        return {
            "symbol": symbol,
            "from": from_time,
            "to": to_time,
            "timeBucket": "1m",
            "targetBins": requested_target,
            "bucketCount": 0,
            "priceBinSize": source_price_bin_size or 0,
            "sourcePriceBinSize": source_price_bin_size,
            "sourceBinCount": len(rows),
            "source": source,
            "feed": feed,
            "feedProfile": feed_profile,
            "calculationVersion": VOLUME_PROFILE_CALCULATION_VERSION,
            "dataStatus": "empty",
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

    domain_min, domain_max, step = nice_display_domain(bucket_range[0], bucket_range[1], requested_target)
    buckets = build_display_buckets(domain_min, domain_max, step, rows)
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
        "from": from_time,
        "to": to_time,
        "timeBucket": "1m",
        "targetBins": requested_target,
        "bucketCount": len(buckets),
        "priceBinSize": rounded(step),
        "sourcePriceBinSize": source_price_bin_size,
        "sourceBinCount": len(rows),
        "source": source,
        "feed": feed,
        "feedProfile": feed_profile,
        "calculationVersion": VOLUME_PROFILE_CALCULATION_VERSION,
        "dataStatus": "ready" if total_volume > 0 else "empty",
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


def normalize_source_bins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = number_or_none(row.get("priceBin"))
        if price is None:
            continue
        size = number_or_none(row.get("priceBinSize")) or 0
        volume = number_or_none(row.get("volume")) or 0
        trade_count = int(number_or_none(row.get("tradeCount")) or 0)
        if volume < 0:
            continue
        normalized.append({
            **row,
            "priceBin": price,
            "priceBinSize": size,
            "priceMin": price,
            "priceMax": price + size if size > 0 else price,
            "priceMid": price + (size / 2 if size > 0 else 0),
            "volume": volume,
            "tradeCount": trade_count,
            "vwap": number_or_none(row.get("vwap")),
        })
    return normalized


def normalize_target_bins(value: int | float | str | None) -> int:
    try:
        parsed = int(value if value is not None else DEFAULT_VOLUME_PROFILE_TARGET_BINS)
    except (TypeError, ValueError):
        parsed = DEFAULT_VOLUME_PROFILE_TARGET_BINS
    return max(4, min(parsed, 48))


def resolve_bucket_range(rows: list[dict[str, Any]], price_min: float | None, price_max: float | None) -> tuple[float, float] | None:
    if not rows:
        return None
    requested_min = number_or_none(price_min)
    requested_max = number_or_none(price_max)
    if requested_min is not None and requested_max is not None:
        if requested_max <= requested_min:
            return None
        return requested_min, requested_max
    min_price = requested_min if requested_min is not None else min(row["priceMin"] for row in rows)
    max_price = requested_max if requested_max is not None else max(row["priceMax"] for row in rows)
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


def build_display_buckets(domain_min: float, domain_max: float, step: float, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    count = bucket_count_for_domain(domain_min, domain_max, step)
    buckets = []
    weighted_vwap = [0.0 for _ in range(count)]
    for index in range(count):
        lower = domain_min + index * step
        upper = lower + step
        buckets.append({
            "index": index,
            "priceBin": rounded(lower),
            "priceBinSize": rounded(step),
            "priceMin": rounded(lower),
            "priceMax": rounded(upper),
            "priceMid": rounded((lower + upper) / 2),
            "volume": 0.0,
            "tradeCount": 0,
            "vwap": None,
            "volumePercent": 0,
            "isPoc": False,
            "inValueArea": False,
        })
    for row in rows:
        center = row["priceMid"]
        if center < domain_min or center > domain_max:
            continue
        index = min(count - 1, max(0, int((center - domain_min) / step)))
        bucket = buckets[index]
        volume = row["volume"]
        bucket["volume"] += volume
        bucket["tradeCount"] += row["tradeCount"]
        if row["vwap"] is not None and volume > 0:
            weighted_vwap[index] += row["vwap"] * volume
    for index, bucket in enumerate(buckets):
        volume = bucket["volume"]
        bucket["volume"] = rounded(volume)
        bucket["tradeCount"] = int(bucket["tradeCount"])
        bucket["vwap"] = rounded(weighted_vwap[index] / volume) if volume > 0 and weighted_vwap[index] > 0 else None
    return buckets


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


def first_number(rows: list[dict[str, Any]], key: str) -> float | None:
    for row in rows:
        value = number_or_none(row.get(key))
        if value is not None:
            return value
    return None


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
