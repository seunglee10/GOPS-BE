from __future__ import annotations

from typing import Any, Iterable

from market_data.serving.time_utils import canonical_utc_timestamp, parse_utc_time


SAME_BUCKET_PROVISIONAL_INTERVALS = {"1D", "1W", "1M"}


def timestamp_millis(value: Any) -> int | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    parsed = parse_utc_time(value)
    if not parsed:
        return None
    return int(parsed.timestamp() * 1000)


def canonical_watermark_value(value: Any) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return canonical_utc_timestamp(value)


def latest_watermark_value(*values: Any) -> str | None:
    latest_score = None
    latest_value = None
    for value in values:
        canonical = canonical_watermark_value(value)
        score = timestamp_millis(canonical)
        if score is None:
            continue
        if latest_score is None or score > latest_score:
            latest_score = score
            latest_value = canonical
    return latest_value


def watermark_after(existing: Any, candidate: Any) -> bool:
    existing_score = timestamp_millis(existing)
    candidate_score = timestamp_millis(candidate)
    if candidate_score is None:
        return False
    return existing_score is None or candidate_score > existing_score


def candle_watermark_value(candle: dict[str, Any] | None) -> str | None:
    if not isinstance(candle, dict):
        return None
    return canonical_watermark_value(candle.get("timestamp"))


def candle_observed_value(candle: dict[str, Any] | None) -> str | None:
    if not isinstance(candle, dict):
        return None
    return latest_watermark_value(
        candle.get("updatedAt"),
        candle.get("receivedAt"),
        candle.get("createdAt"),
    )


def candle_at_or_before_watermark(candle: dict[str, Any] | None, watermark: Any) -> bool:
    candle_score = timestamp_millis(candle_watermark_value(candle))
    watermark_score = timestamp_millis(watermark)
    if candle_score is None or watermark_score is None:
        return False
    return candle_score <= watermark_score


def latest_closed_candle_from_groups(*groups: Iterable[dict[str, Any]] | None) -> dict[str, Any] | None:
    latest_score = None
    latest_candle = None
    for group in groups:
        for candle in group or []:
            if not isinstance(candle, dict):
                continue
            if candle.get("isClosed", True) is False:
                continue
            score = timestamp_millis(candle_watermark_value(candle))
            if score is None:
                continue
            if latest_score is None or score > latest_score:
                latest_score = score
                latest_candle = candle
    return latest_candle


def latest_closed_watermark_from_candles(*groups: Iterable[dict[str, Any]] | None) -> str | None:
    timestamps = []
    for group in groups:
        for candle in group or []:
            if not isinstance(candle, dict):
                continue
            if candle.get("isClosed", True) is False:
                continue
            timestamps.append(candle.get("timestamp"))
    return latest_watermark_value(*timestamps)


def live_candle_at_or_before_watermark(
    live_candle: dict[str, Any] | None,
    watermark: Any,
    latest_closed_candle: dict[str, Any] | None = None,
) -> bool:
    candle_score = timestamp_millis(candle_watermark_value(live_candle))
    watermark_score = timestamp_millis(watermark)
    if candle_score is None or watermark_score is None:
        return False
    if candle_score < watermark_score:
        return True
    if candle_score > watermark_score:
        return False
    if _same_bucket_provisional_live_is_newer(live_candle, latest_closed_candle):
        return False
    return True


def _same_bucket_provisional_live_is_newer(
    live_candle: dict[str, Any] | None,
    latest_closed_candle: dict[str, Any] | None,
) -> bool:
    if not isinstance(live_candle, dict):
        return False
    if live_candle.get("isClosed", True) is not False:
        return False
    if live_candle.get("interval") not in SAME_BUCKET_PROVISIONAL_INTERVALS:
        return False
    if latest_closed_candle is None:
        return True
    closed_observed = candle_observed_value(latest_closed_candle)
    live_observed = candle_observed_value(live_candle)
    if not closed_observed or not live_observed:
        return False
    return watermark_after(closed_observed, live_observed)


def live_candle_after_latest_closed(
    live_candle: dict[str, Any] | None,
    *closed_groups: Iterable[dict[str, Any]] | None,
    watermark: Any = None,
) -> dict[str, Any] | None:
    if not live_candle:
        return None
    latest_closed = latest_watermark_value(
        watermark,
        latest_closed_watermark_from_candles(*closed_groups),
    )
    latest_closed_candle = latest_closed_candle_from_groups(*closed_groups)
    if live_candle_at_or_before_watermark(live_candle, latest_closed, latest_closed_candle):
        return None
    return live_candle
