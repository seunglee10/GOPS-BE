from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from alfaka.backfill.gapfill import TradingCalendar
from alfaka.serving.intervals import INTRADAY_DERIVED_INTERVALS, INTRADAY_INTERVAL_MINUTES
from alfaka.serving.time_utils import parse_utc_time


BUCKET_POLICY_CLOCK_ALIGNED = "clock_aligned"
BUCKET_POLICY_SOURCE_NATIVE = "source_native"
BUCKET_POLICY_REGULAR_SESSION = "us_equity_regular_session"


@dataclass(frozen=True)
class SessionBucket:
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


def aggregate_regular_session_candles(
    rows: Iterable[dict[str, Any]],
    interval: str,
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
) -> list[dict[str, Any]]:
    """Aggregate real regular-session 1m rows without manufacturing empty bars."""
    if interval not in INTRADAY_DERIVED_INTERVALS:
        raise ValueError(f"Regular-session aggregation does not support {interval}")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    trading_calendar = calendar or TradingCalendar.from_environment()
    grouped: dict[str, tuple[SessionBucket, list[dict[str, Any]]]] = {}
    for source in rows:
        if source.get("isClosed", source.get("is_closed", True)) is False:
            continue
        if source.get("marketSession", source.get("market_session", "regular")) not in {None, "", "regular"}:
            continue
        timestamp = source.get("timestamp") or source.get("event_time")
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
        candles = sorted(source_rows, key=lambda row: _parsed_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc))
        if not candles:
            continue
        first, latest = candles[0], candles[-1]
        volume = sum(float(row.get("volume") or 0) for row in candles)
        trade_count_values = [row.get("tradeCount", row.get("trade_count")) for row in candles]
        trade_count = sum(int(value or 0) for value in trade_count_values)
        weighted_vwap = sum(
            float(row.get("vwap") or 0) * float(row.get("volume") or 0)
            for row in candles
            if row.get("vwap") is not None
        )
        symbol = str(latest.get("symbol") or first.get("symbol") or "").upper()
        event_id_seed = f"{symbol}|{interval}|{timestamp}|{len(candles)}"
        result.append({
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
            "isClosed": True,
            "correctionType": latest.get("correctionType", latest.get("correction_type", "NONE")),
            "source": "derived.regular-session",
            "sourceClass": "derived_aggregate",
            "sourceInterval": "1m",
            "feed": latest.get("feed") or "unknown",
            "feedProfile": latest.get("feedProfile", latest.get("feed_profile")) or latest.get("feed") or "unknown",
            "marketSession": "regular",
            "priceAdjustment": latest.get("priceAdjustment", latest.get("price_adjustment", "split")),
            "canonicalVersion": latest.get("canonicalVersion", latest.get("canonical_version", "v2")),
            "bucketPolicy": BUCKET_POLICY_REGULAR_SESSION,
            "sourceEventId": f"derived/{interval}/{hashlib.sha256(event_id_seed.encode()).hexdigest()[:24]}",
            "createdAt": latest.get("createdAt", latest.get("created_at")),
        })
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


def _parsed_timestamp(row: dict[str, Any]) -> datetime | None:
    return parse_utc_time(row.get("timestamp") or row.get("event_time"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
