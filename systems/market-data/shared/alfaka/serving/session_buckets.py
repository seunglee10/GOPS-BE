from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from alfaka.alpaca.feed_profiles import active_extended_session_window, visible_extended_session_windows
from alfaka.backfill.gapfill import TradingCalendar
from alfaka.serving.intervals import INTRADAY_DERIVED_INTERVALS, INTRADAY_INTERVAL_MINUTES
from alfaka.serving.time_utils import parse_utc_time


BUCKET_POLICY_CLOCK_ALIGNED = "clock_aligned"
BUCKET_POLICY_SOURCE_NATIVE = "source_native"
BUCKET_POLICY_REGULAR_SESSION = "us_equity_regular_session"
BUCKET_POLICY_EXTENDED_SESSION = "us_equity_extended_session"


@dataclass(frozen=True)
class SessionBucket:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ExtendedSessionBucket:
    session: str
    start: datetime
    end: datetime


def regular_session_bucket(
    value: Any,
    interval: str,
    *,
    calendar: TradingCalendar | None = None,
) -> SessionBucket | None:
    """Return the NY regular-session bucket containing ``value``.

    The bucket is anchored at the actual session open rather than a UTC clock
    boundary.  ``TradingCalendar`` owns holidays, DST through its timezone, and
    early-close times.
    """
    if interval not in INTRADAY_DERIVED_INTERVALS:
        raise ValueError(f"Regular-session aggregation does not support {interval}")
    parsed = parse_utc_time(value)
    if parsed is None:
        return None
    trading_calendar = calendar or TradingCalendar.from_environment()
    local = parsed.astimezone(trading_calendar.timezone)
    session_day = local.date()
    if not trading_calendar.is_session_date(session_day):
        return None
    opened = datetime.combine(session_day, trading_calendar.open_time, trading_calendar.timezone)
    closed = datetime.combine(
        session_day,
        trading_calendar.session_close_for(session_day),
        trading_calendar.timezone,
    )
    if local < opened or local >= closed:
        return None
    elapsed_minutes = int((local - opened).total_seconds() // 60)
    bucket_minutes = INTRADAY_INTERVAL_MINUTES[interval]
    start = opened + timedelta(minutes=(elapsed_minutes // bucket_minutes) * bucket_minutes)
    end = min(start + timedelta(minutes=bucket_minutes), closed)
    return SessionBucket(start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc))


def extended_session_bucket(value: Any, interval: str) -> ExtendedSessionBucket | None:
    """Return the pre/after/overnight bucket containing ``value``."""
    if interval not in INTRADAY_DERIVED_INTERVALS:
        raise ValueError(f"Extended-session aggregation does not support {interval}")
    parsed = parse_utc_time(value)
    if parsed is None:
        return None
    window = active_extended_session_window(parsed)
    if window is None:
        return None
    return _extended_session_bucket_from_window(parsed, interval, window)


def aggregate_regular_session_candles(
    rows: Iterable[dict[str, Any]],
    interval: str,
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
    source_interval: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate real regular-session source rows without manufacturing empty bars."""
    if interval not in INTRADAY_DERIVED_INTERVALS:
        raise ValueError(f"Regular-session aggregation does not support {interval}")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    trading_calendar = calendar or TradingCalendar.from_environment()
    resolved_source_interval = source_interval
    grouped: dict[str, tuple[SessionBucket, list[dict[str, Any]]]] = {}
    for source in rows:
        if source.get("isClosed", source.get("is_closed", True)) is False:
            continue
        if source.get("marketSession", source.get("market_session", "regular")) not in {None, "", "regular"}:
            continue
        timestamp = source.get("timestamp") or source.get("event_time")
        resolved_source_interval = resolved_source_interval or source.get("interval") or source.get("sourceInterval")
        bucket = regular_session_bucket(timestamp, interval, calendar=trading_calendar)
        if bucket is None:
            continue
        key = _iso(bucket.start)
        grouped.setdefault(key, (bucket, []))[1].append(dict(source))

    result: list[dict[str, Any]] = []
    for timestamp in sorted(grouped):
        bucket, source_rows = grouped[timestamp]
        if bucket.end > reference:
            continue
        candle = _build_aggregated_candle(
            source_rows,
            interval=interval,
            timestamp=timestamp,
            market_session="regular",
            bucket_policy=BUCKET_POLICY_REGULAR_SESSION,
            source="derived.regular-session",
            source_interval=resolved_source_interval or "1m",
            is_closed=True,
        )
        if candle:
            result.append(candle)
    return result


def aggregate_visible_extended_session_candles(
    rows: Iterable[dict[str, Any]],
    interval: str,
    *,
    now: datetime | None = None,
    source_interval: str = "1m",
) -> list[dict[str, Any]]:
    """Aggregate only the current and contiguous visible extended sessions."""
    if interval not in INTRADAY_DERIVED_INTERVALS:
        raise ValueError(f"Extended-session aggregation does not support {interval}")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windows = visible_extended_session_windows(reference)
    if not windows:
        return []

    grouped: dict[str, tuple[ExtendedSessionBucket, list[dict[str, Any]]]] = {}
    for source in rows:
        parsed = _parsed_timestamp(source)
        if parsed is None:
            continue
        declared_session = str(source.get("marketSession", source.get("market_session", "")) or "").lower()
        for window in windows:
            session, start, end = window
            if not (start <= parsed < end):
                continue
            if declared_session and declared_session != session:
                break
            bucket = _extended_session_bucket_from_window(parsed, interval, window)
            if bucket is None:
                break
            key = f"{session}|{_iso(bucket.start)}"
            grouped.setdefault(key, (bucket, []))[1].append(dict(source))
            break

    result: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda value: value.split("|", 1)[1]):
        bucket, source_rows = grouped[key]
        candle = _build_aggregated_candle(
            source_rows,
            interval=interval,
            timestamp=_iso(bucket.start),
            market_session=bucket.session,
            bucket_policy=BUCKET_POLICY_EXTENDED_SESSION,
            source="derived.extended-session",
            source_interval=source_interval,
            is_closed=bucket.end <= reference,
        )
        if candle:
            result.append(candle)
    return result


def aggregate_extended_session_candles(
    rows: Iterable[dict[str, Any]],
    interval: str,
    *,
    now: datetime | None = None,
    source_interval: str = "1m",
) -> list[dict[str, Any]]:
    """Aggregate every real pre/after/overnight source row, including historical sessions."""
    if interval not in INTRADAY_DERIVED_INTERVALS:
        raise ValueError(f"Extended-session aggregation does not support {interval}")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    grouped: dict[str, tuple[ExtendedSessionBucket, list[dict[str, Any]]]] = {}
    for source in rows:
        parsed = _parsed_timestamp(source)
        if parsed is None:
            continue
        bucket = extended_session_bucket(parsed, interval)
        if bucket is None:
            continue
        declared_session = str(source.get("marketSession", source.get("market_session", "")) or "").lower()
        if declared_session and declared_session != bucket.session:
            continue
        key = f"{bucket.session}|{_iso(bucket.start)}"
        grouped.setdefault(key, (bucket, []))[1].append(dict(source))

    result: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda value: value.split("|", 1)[1]):
        bucket, source_rows = grouped[key]
        candle = _build_aggregated_candle(
            source_rows,
            interval=interval,
            timestamp=_iso(bucket.start),
            market_session=bucket.session,
            bucket_policy=BUCKET_POLICY_EXTENDED_SESSION,
            source="derived.extended-session",
            source_interval=source_interval,
            is_closed=bucket.end <= reference,
        )
        if candle:
            result.append(candle)
    return result


def bucket_policy_for_candle(payload: dict[str, Any]) -> str:
    explicit = payload.get("bucketPolicy", payload.get("bucket_policy"))
    if explicit:
        return str(explicit)
    interval = str(payload.get("interval") or "1m")
    source = str(payload.get("source") or "")
    source_interval = str(payload.get("sourceInterval", payload.get("source_interval")) or "")
    if interval in INTRADAY_DERIVED_INTERVALS and (
        source_interval == "1m" or source.startswith("derived.regular-session")
    ):
        return BUCKET_POLICY_REGULAR_SESSION
    if interval in {"1m", "1D", "1d"}:
        return BUCKET_POLICY_SOURCE_NATIVE
    return BUCKET_POLICY_CLOCK_ALIGNED


def _extended_session_bucket_from_window(
    parsed: datetime,
    interval: str,
    window: tuple[str, datetime, datetime],
) -> ExtendedSessionBucket | None:
    session, start, end = window
    if not (start <= parsed < end):
        return None
    bucket_minutes = INTRADAY_INTERVAL_MINUTES[interval]
    elapsed_minutes = int((parsed - start).total_seconds() // 60)
    bucket_start = start + timedelta(minutes=(elapsed_minutes // bucket_minutes) * bucket_minutes)
    return ExtendedSessionBucket(
        session=session,
        start=bucket_start,
        end=min(bucket_start + timedelta(minutes=bucket_minutes), end),
    )


def _build_aggregated_candle(
    source_rows: Iterable[dict[str, Any]],
    *,
    interval: str,
    timestamp: str,
    market_session: str,
    bucket_policy: str,
    source: str,
    source_interval: str,
    is_closed: bool,
) -> dict[str, Any] | None:
    candles = sorted(
        source_rows,
        key=lambda row: _parsed_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc),
    )
    if not candles:
        return None
    first, latest = candles[0], candles[-1]
    volume = sum(float(row.get("volume") or 0) for row in candles)
    trade_count = sum(int(row.get("tradeCount", row.get("trade_count")) or 0) for row in candles)
    weighted_vwap = sum(
        float(row.get("vwap") or 0) * float(row.get("volume") or 0)
        for row in candles
        if row.get("vwap") is not None
    )
    symbol = str(latest.get("symbol") or first.get("symbol") or "").upper()
    if market_session == "regular":
        event_id_seed = f"{symbol}|{interval}|{timestamp}|{len(candles)}"
    else:
        event_id_seed = f"{symbol}|{interval}|{market_session}|{timestamp}|{len(candles)}"
    return {
        "eventType": "CANDLE",
        "symbol": symbol,
        "interval": interval,
        "timestamp": timestamp,
        "open": float(first["open"]),
        "high": max(float(row["high"]) for row in candles),
        "low": min(float(row["low"]) for row in candles),
        "close": float(latest["close"]),
        "volume": volume,
        "tradeCount": trade_count,
        "vwap": weighted_vwap / volume if volume > 0 and weighted_vwap > 0 else latest.get("vwap"),
        "ma": {},
        "isClosed": is_closed,
        "correctionType": latest.get("correctionType", latest.get("correction_type", "NONE")),
        "source": source,
        "sourceClass": "derived_aggregate",
        "sourceInterval": source_interval,
        "feed": latest.get("feed") or "unknown",
        "feedProfile": latest.get("feedProfile", latest.get("feed_profile")) or latest.get("feed") or "unknown",
        "marketSession": market_session,
        "priceAdjustment": latest.get("priceAdjustment", latest.get("price_adjustment", "split")),
        "canonicalVersion": latest.get("canonicalVersion", latest.get("canonical_version", "v2")),
        "bucketPolicy": bucket_policy,
        "sourceEventId": f"derived/{interval}/{hashlib.sha256(event_id_seed.encode()).hexdigest()[:24]}",
        "createdAt": latest.get("createdAt", latest.get("created_at")),
    }


def _parsed_timestamp(row: dict[str, Any]) -> datetime | None:
    return parse_utc_time(row.get("timestamp") or row.get("event_time"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
