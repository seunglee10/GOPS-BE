from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from alfaka.backfill.gapfill import TradingCalendar
from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time
from alfaka.serving.intervals import INTRADAY_INTERVAL_MINUTES
from alfaka.serving.session_buckets import regular_session_bucket
from alfaka.storage.candle_validation import invalid_candle_numeric_reason


MARKET_TIMEZONE = ZoneInfo("America/New_York")
CANONICAL_DATA_VERSION = "v2"
SESSION_POLICY = "us-equity-regular"
ADJUSTMENT_POLICY = "split"
CANDLE_CONTRACT_VERSION = "regular-session-derived"
INTRADAY_ANALYSIS_INTERVALS = tuple(INTRADAY_INTERVAL_MINUTES)
LONG_ANALYSIS_INTERVALS = ("1D", "1W", "1M")
ANALYSIS_INTERVALS = (*INTRADAY_ANALYSIS_INTERVALS, *LONG_ANALYSIS_INTERVALS)
SOURCE_RANK_ANALYSIS = {"derived_aggregate": 1, "clickhouse_direct": 2}
SOURCE_RANK_CHART_COMPLETED = {
    "derived_aggregate": 1,
    "clickhouse_direct": 2,
    "redis_closed": 3,
}
SOURCE_RANK_CURRENT = {
    "derived_aggregate": 1,
    "clickhouse_direct": 2,
    "redis_closed": 3,
    "active_live": 4,
}


@dataclass(frozen=True)
class AnalysisCandleBundle:
    rows: dict[str, list[dict[str, Any]]]
    coverage: dict[str, dict[str, Any]]
    digests: dict[str, str]


@dataclass(frozen=True)
class AnalysisDailyWindow:
    start: str
    end: str
    expected_keys: tuple[str, ...]


def canonicalize_candle_identity(
    row: dict[str, Any], interval: str, session_policy: str = SESSION_POLICY
) -> dict[str, Any] | None:
    if session_policy != SESSION_POLICY or interval not in ANALYSIS_INTERVALS:
        raise ValueError("Unsupported analysis candle identity policy")
    parsed = parse_utc_time(row.get("timestamp") or row.get("event_time"))
    if parsed is None:
        return None
    if interval in INTRADAY_ANALYSIS_INTERVALS:
        timestamp = _iso_utc_milliseconds(parsed)
        normalized = dict(row)
        normalized.update({"interval": interval, "candleKey": timestamp, "timestamp": timestamp})
        return normalized
    market_day = parsed.astimezone(MARKET_TIMEZONE).date()
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        market_day = parsed.date()
    if interval == "1D":
        key_day = market_day
        candle_key = key_day.isoformat()
    elif interval == "1W":
        key_day = market_day - timedelta(days=market_day.weekday())
        candle_key = key_day.isoformat()
    else:
        key_day = market_day.replace(day=1)
        candle_key = key_day.strftime("%Y-%m")
    timestamp = _key_timestamp(candle_key, interval)
    normalized = dict(row)
    normalized.update({"interval": interval, "candleKey": candle_key, "timestamp": timestamp})
    return normalized


def choose_canonical_winner(
    rows: Iterable[dict[str, Any]], *, view: str = "analysis_closed"
) -> dict[str, Any] | None:
    ranks = (
        SOURCE_RANK_ANALYSIS if view == "analysis_closed"
        else SOURCE_RANK_CHART_COMPLETED if view == "chart_completed"
        else SOURCE_RANK_CURRENT
    )
    winner: dict[str, Any] | None = None
    winner_rank: tuple[int, float, str] | None = None
    winner_hash: str | None = None
    for row in rows:
        if row.get("canonicalVersion", row.get("canonical_version", CANONICAL_DATA_VERSION)) != CANONICAL_DATA_VERSION:
            continue
        if row.get("priceAdjustment", row.get("price_adjustment", ADJUSTMENT_POLICY)) != ADJUSTMENT_POLICY:
            continue
        session = row.get("marketSession", row.get("market_session", "regular"))
        if session not in {None, "", "regular"}:
            continue
        closed_value = row.get("isClosed", row.get("is_closed"))
        is_closed = closed_value is not False if closed_value is not None else view != "chart_completed"
        if view in {"analysis_closed", "chart_completed"} and not is_closed:
            continue
        source = _source_class(row, is_closed)
        if source not in ranks:
            continue
        rank = (
            ranks[source],
            _revision_epoch(row),
            str(row.get("sourceEventId") or row.get("source_event_id") or ""),
        )
        if winner_rank is None or rank > winner_rank:
            winner = row
            winner_rank = rank
            winner_hash = None
            continue
        if rank != winner_rank:
            continue
        # Most candle keys have one eligible row. Hash only an exact precedence
        # tie so deterministic winner selection does not tax the common path.
        row_hash = _stable_hash(row)
        winner_hash = winner_hash or _stable_hash(winner)
        if row_hash > winner_hash:
            winner = row
            winner_hash = row_hash
    return dict(winner) if winner is not None else None


def merge_canonical_candles(
    *groups: Iterable[dict[str, Any]], interval: str, view: str = "chart_current"
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for group in groups:
        for source in group:
            normalized = canonicalize_candle_identity(source, interval)
            if normalized is None:
                continue
            symbol = str(normalized.get("symbol") or "").upper()
            key = (symbol, interval, normalized["candleKey"])
            grouped.setdefault(key, []).append(normalized)
    winners = [choose_canonical_winner(values, view=view) for values in grouped.values()]
    return sorted(
        (row for row in winners if row),
        key=lambda row: (
            row["timestamp"],
            str(row.get("symbol") or ""),
            str(row.get("candleKey") or ""),
        ),
    )


def is_analysis_candle_bucket_complete(
    timestamp: Any,
    interval: str,
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
) -> bool:
    identity = canonicalize_candle_identity({"timestamp": timestamp}, interval)
    if identity is None:
        return False
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if interval in INTRADAY_ANALYSIS_INTERVALS:
        bucket = parse_utc_time(identity["timestamp"])
        if bucket is None:
            return False
        if interval == "1m":
            return bucket + timedelta(minutes=1) <= reference
        session_bucket = regular_session_bucket(bucket, interval, calendar=calendar)
        return session_bucket is not None and session_bucket.end <= reference
    trading_calendar = calendar or TradingCalendar.from_environment()
    completed_at = _bucket_completed_at(
        _bucket_date(identity["candleKey"], interval),
        interval,
        trading_calendar,
    )
    return completed_at is not None and completed_at <= reference


def aggregate_analysis_candles(
    daily_rows: list[dict[str, Any]],
    interval: str,
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
    view: str = "analysis_closed",
) -> list[dict[str, Any]]:
    canonical = merge_canonical_candles(daily_rows, interval="1D", view=view)
    return _aggregate_canonical_daily(canonical, interval, now=now, calendar=calendar)


def aggregate_analysis_candle_bundle(
    daily_rows: list[dict[str, Any]],
    intervals: Iterable[str],
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
    view: str = "analysis_closed",
) -> dict[str, list[dict[str, Any]]]:
    """Derive requested analysis intervals after one canonical 1D merge."""
    requested = tuple(dict.fromkeys(intervals))
    if not requested or set(requested).difference({"1D", "1W", "1M"}):
        raise ValueError("Unsupported analysis intervals")
    canonical = merge_canonical_candles(daily_rows, interval="1D", view=view)
    return {
        interval: _aggregate_canonical_daily(canonical, interval, now=now, calendar=calendar)
        for interval in requested
    }


def _aggregate_canonical_daily(
    canonical: list[dict[str, Any]],
    interval: str,
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
) -> list[dict[str, Any]]:
    if interval == "1D":
        return [_analysis_row(row, "1D", index) for index, row in enumerate(canonical)]
    if interval not in {"1W", "1M"}:
        raise ValueError(f"Unsupported analysis interval: {interval}")
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in canonical:
        # Canonical daily rows already carry the NY trading-date identity. The
        # higher-timeframe bucket can be derived from that key without parsing
        # and formatting the timestamp again for every requested interval.
        daily_key = str(row.get("candleKey") or "")
        try:
            trading_day = date.fromisoformat(daily_key)
        except ValueError:
            continue
        if interval == "1W":
            key = (trading_day - timedelta(days=trading_day.weekday())).isoformat()
        else:
            key = daily_key[:7]
        buckets.setdefault(key, []).append(row)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    trading_calendar = calendar or TradingCalendar.from_environment()
    last_completed_key = _last_expected_key(interval, reference, calendar=trading_calendar)
    if last_completed_key is None:
        return []
    result: list[dict[str, Any]] = []
    for key in sorted(buckets):
        rows = sorted(buckets[key], key=lambda item: item["timestamp"])
        # Only the newest calendar bucket can be incomplete. Resolving that key
        # once avoids a calendar/timestamp parse for every historical bucket.
        if key > last_completed_key:
            continue
        aggregate = {
            **rows[0],
            "timestamp": _key_timestamp(key, interval),
            "candleKey": key,
            "interval": interval,
            "open": float(rows[0]["open"]),
            "high": max(float(row["high"]) for row in rows),
            "low": min(float(row["low"]) for row in rows),
            "close": float(rows[-1]["close"]),
            "volume": sum(float(row.get("volume") or 0) for row in rows),
            "isClosed": True,
            "sourceClass": "derived_aggregate",
        }
        result.append(_analysis_row(aggregate, interval, len(result)))
    return result


def analysis_input_digest(symbol: str, interval: str, rows: list[dict[str, Any]]) -> str:
    payload = {
        "symbol": symbol.upper(), "interval": interval,
        "candleContractVersion": CANDLE_CONTRACT_VERSION,
        "canonicalDataVersion": CANONICAL_DATA_VERSION,
        "sessionPolicy": SESSION_POLICY, "adjustmentPolicy": ADJUSTMENT_POLICY,
        "candles": [[row.get("candleKey"), row.get("timestamp"), *[_decimal(row.get(key)) for key in ("open", "high", "low", "close", "volume")], bool(row.get("isClosed", True))] for row in rows],
    }
    return "sha256:" + hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def compute_analysis_coverage(
    rows: list[dict[str, Any]],
    interval: str,
    *,
    display_bars: int,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    actual = {str(row.get("candleKey")) for row in rows if row.get("candleKey")}
    trading_calendar = calendar or TradingCalendar.from_environment()
    if interval in INTRADAY_ANALYSIS_INTERVALS:
        expected = _expected_intraday_keys(reference, display_bars, interval, trading_calendar)
        return _coverage_result(expected, actual, interval, reference)
    if not rows:
        return _coverage_result([], actual, interval, reference)
    last_key = _last_expected_key(interval, reference, calendar=trading_calendar)
    if last_key is None:
        expected: list[str] = []
    else:
        expected = _expected_keys_ending(interval, last_key, display_bars, calendar=trading_calendar)
    return _coverage_result(expected, actual, interval, reference)


class AnalysisCandleSource:
    def __init__(self, provider: Any, *, now_provider=None, view: str = "analysis_closed"):
        if view not in {"analysis_closed", "chart_completed"}:
            raise ValueError("Unsupported analysis candle source view")
        self.provider = provider
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.view = view

    def load_symbol(self, symbol: str, requested_intervals: Iterable[str]) -> AnalysisCandleBundle:
        intervals = tuple(dict.fromkeys(requested_intervals))
        if not intervals or set(intervals).difference(ANALYSIS_INTERVALS):
            raise ValueError("Unsupported analysis intervals")
        from .schema import DISPLAY_BARS, LOOKBACK_BARS
        now = self.now_provider()
        rows: dict[str, list[dict[str, Any]]] = {}
        long_intervals = tuple(item for item in intervals if item in LONG_ANALYSIS_INTERVALS)
        if long_intervals:
            window = analysis_daily_window(long_intervals, now=now)
            raw = self.provider.daily_candles(
                symbol,
                interval="1D",
                limit=max(1, len(window.expected_keys) + 16),
                from_time=window.start,
                before=window.end,
            )
            aggregated = aggregate_analysis_candle_bundle(raw, long_intervals, now=now, view=self.view)
            rows.update({item: aggregated[item][-LOOKBACK_BARS[item]:] for item in long_intervals})
        for interval in intervals:
            if interval not in INTRADAY_ANALYSIS_INTERVALS:
                continue
            raw = self.provider.stored_interval_candles(
                symbol,
                interval,
                limit=LOOKBACK_BARS[interval] + 16,
            )
            canonical = merge_canonical_candles(raw, interval=interval, view=self.view)
            rows[interval] = [
                _analysis_row(row, interval, index)
                for index, row in enumerate(canonical[-LOOKBACK_BARS[interval]:])
            ]
        rows = {item: rows[item] for item in intervals}
        coverage = {item: compute_analysis_coverage(rows[item], item, display_bars=LOOKBACK_BARS[item], now=now) for item in intervals}
        digests = {item: analysis_input_digest(symbol, item, rows[item]) for item in intervals}
        return AnalysisCandleBundle(rows=rows, coverage=coverage, digests=digests)


def analysis_daily_window(
    requested_intervals: Iterable[str],
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
) -> AnalysisDailyWindow:
    from .schema import LOOKBACK_BARS

    intervals = tuple(dict.fromkeys(requested_intervals))
    if not intervals or set(intervals).difference({"1D", "1W", "1M"}):
        raise ValueError("Unsupported analysis intervals")
    reference = now or datetime.now(timezone.utc)
    trading_calendar = calendar or TradingCalendar.from_environment()
    candidates: list[tuple[date, date]] = []
    if "1D" in intervals:
        last_key = _last_expected_key("1D", reference, calendar=trading_calendar)
        if last_key:
            sessions = _expected_keys_ending("1D", last_key, LOOKBACK_BARS["1D"], calendar=trading_calendar)
            candidates.append((date.fromisoformat(sessions[0]), date.fromisoformat(sessions[-1]) + timedelta(days=1)))
    if "1W" in intervals:
        last_key = _last_expected_key("1W", reference, calendar=trading_calendar)
        if last_key:
            last_week = date.fromisoformat(last_key)
            first_week = last_week - timedelta(days=7 * (LOOKBACK_BARS["1W"] - 1))
            candidates.append((first_week, last_week + timedelta(days=7)))
    if "1M" in intervals:
        last_key = _last_expected_key("1M", reference, calendar=trading_calendar)
        if last_key:
            last_month = date.fromisoformat(last_key + "-01")
            first_month = _shift_month(last_month, -(LOOKBACK_BARS["1M"] - 1))
            candidates.append((first_month, _shift_month(last_month, 1)))
    if not candidates:
        raise ValueError("Unable to resolve analysis daily window")
    start_day = min(item[0] for item in candidates)
    end_day = max(item[1] for item in candidates)
    expected: list[str] = []
    cursor = start_day
    while cursor < end_day:
        if trading_calendar.is_session_date(cursor):
            expected.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return AnalysisDailyWindow(
        # Query on UTC date boundaries so both legacy 00:00Z rows and current
        # market-midnight rows for the same trading date remain visible. Candle
        # identity is normalized separately after the read.
        start=_utc_midnight(start_day),
        end=_utc_midnight(end_day),
        expected_keys=tuple(expected),
    )


def _analysis_row(row: dict[str, Any], interval: str, index: int) -> dict[str, Any]:
    reason = invalid_candle_numeric_reason(row, require=True)
    if reason:
        raise ValueError(f"Invalid canonical analysis candle values: {reason}")
    try:
        values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        volume = float(row.get("volume") or 0)
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid canonical analysis candle") from None
    return {"candleKey": row["candleKey"], "timestamp": row["timestamp"], "barIndex": index, **values, "volume": volume, "isClosed": True, "interval": interval}


def _coverage_result(expected: list[str], actual: set[str], interval: str, reference: datetime) -> dict[str, Any]:
    hits = [key in actual for key in expected]
    missing = len(hits) - sum(hits)
    contiguous = 0
    for hit in reversed(hits):
        if not hit:
            break
        contiguous += 1
    largest = current = 0
    for hit in hits:
        current = 0 if hit else current + 1
        largest = max(largest, current)
    last_expected = _key_timestamp(expected[-1], interval) if expected else None
    actual_expected = [key for key in expected if key in actual]
    last_actual = _key_timestamp(actual_expected[-1], interval) if actual_expected else None
    ratio = sum(hits) / len(hits) if hits else 0.0
    missing_indexes = [index for index, hit in enumerate(hits) if not hit]
    first_present = next((index for index, hit in enumerate(hits) if hit), len(hits))
    missing_head_only = all(index < first_present for index in missing_indexes)
    if not missing_indexes and expected:
        state = "full"
    elif contiguous >= 120 and missing_head_only and last_actual == last_expected:
        state = "partial"
    else:
        state = "data_insufficient"
    flags = []
    if contiguous < 120: flags.append("recent_contiguous_below_minimum")
    if missing_indexes and not missing_head_only: flags.append("interior_gap")
    if last_expected and last_actual != last_expected: flags.append("stale_input")
    return {
        "expectedBars": len(expected), "actualBars": sum(hits), "missingBars": missing,
        "coverageRatio": round(ratio, 4), "recentContiguousBars": contiguous,
        "largestGapBars": largest, "lastExpectedClosedAt": last_expected,
        "lastActualClosedAt": last_actual, "renderable": state in {"full", "partial"},
        "coverageState": state, "state": state,
        "qualityFlags": flags,
    }


def _expected_keys_ending(
    interval: str,
    last_key: str,
    count: int,
    *,
    calendar: TradingCalendar | None = None,
) -> list[str]:
    cursor = _bucket_date(last_key, interval)
    trading_calendar = calendar or TradingCalendar.from_environment()
    result: list[str] = []
    while len(result) < count:
        if interval == "1D":
            if trading_calendar.is_session_date(cursor): result.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        elif interval == "1W":
            if any(trading_calendar.is_session_date(cursor + timedelta(days=day)) for day in range(5)): result.append(cursor.isoformat())
            cursor -= timedelta(days=7)
        else:
            if any(trading_calendar.is_session_date(cursor + timedelta(days=day)) for day in range(_days_in_month(cursor))): result.append(cursor.strftime("%Y-%m"))
            cursor = (cursor - timedelta(days=1)).replace(day=1)
    return list(reversed(result))


def _last_expected_key(
    interval: str,
    now: datetime,
    *,
    calendar: TradingCalendar | None = None,
) -> str | None:
    trading_calendar = calendar or TradingCalendar.from_environment()
    local = now.astimezone(trading_calendar.timezone)
    day = local.date()
    if interval == "1D":
        if (
            not trading_calendar.is_session_date(day)
            or local.time() < trading_calendar.session_close_for(day)
        ):
            day -= timedelta(days=1)
        while not trading_calendar.is_session_date(day):
            day -= timedelta(days=1)
        return day.isoformat()
    if interval == "1W":
        monday = day - timedelta(days=day.weekday())
        completed_at = _bucket_completed_at(monday, interval, trading_calendar)
        if completed_at is not None and now.astimezone(timezone.utc) >= completed_at:
            return monday.isoformat()
        prior = monday - timedelta(days=7)
        return prior.isoformat()
    month = day.replace(day=1)
    completed_at = _bucket_completed_at(month, interval, trading_calendar)
    if completed_at is not None and now.astimezone(timezone.utc) >= completed_at:
        return month.strftime("%Y-%m")
    prior = (month - timedelta(days=1)).replace(day=1)
    return prior.strftime("%Y-%m")


def _shift_month(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _market_midnight_utc(day: date) -> str:
    return _iso_utc_milliseconds(
        datetime.combine(day, time.min, tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
    )


def _utc_midnight(day: date) -> str:
    return _iso_utc_milliseconds(datetime.combine(day, time.min, tzinfo=timezone.utc))


def _iso_utc_milliseconds(value: datetime) -> str:
    # datetime.strftime is disproportionately expensive on macOS. isoformat
    # preserves the exact contract while keeping request-scoped MTF builds
    # comfortably inside their latency budget.
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bucket_date(key: str, interval: str) -> date:
    return date.fromisoformat(key + "-01") if interval == "1M" else date.fromisoformat(key)


def _bucket_completed_at(
    day: date,
    interval: str,
    calendar: TradingCalendar,
) -> datetime | None:
    if interval == "1D":
        bucket_end = day + timedelta(days=1)
    elif interval == "1W":
        bucket_end = day + timedelta(days=7)
    elif interval == "1M":
        bucket_end = _shift_month(day.replace(day=1), 1)
    else:
        raise ValueError(f"Unsupported analysis interval: {interval}")
    cursor = bucket_end - timedelta(days=1)
    while cursor >= day and not calendar.is_session_date(cursor):
        cursor -= timedelta(days=1)
    if cursor < day:
        return None
    return datetime.combine(
        cursor,
        calendar.session_close_for(cursor),
        tzinfo=calendar.timezone,
    ).astimezone(timezone.utc)


def _key_timestamp(key: str, interval: str) -> str:
    if interval in INTRADAY_ANALYSIS_INTERVALS:
        parsed = parse_utc_time(key)
        return _iso_utc_milliseconds(parsed) if parsed else key
    bucket_date = _bucket_date(key, interval)
    if interval == "1D":
        return _market_midnight_utc(bucket_date)
    return _utc_midnight(bucket_date)


def _expected_intraday_keys(
    reference: datetime,
    count: int,
    interval: str,
    calendar: TradingCalendar,
) -> list[str]:
    step = timedelta(minutes=INTRADAY_INTERVAL_MINUTES[interval])
    local_reference = reference.astimezone(calendar.timezone)
    session_day = local_reference.date()
    values: list[datetime] = []
    while len(values) < count:
        if calendar.is_session_date(session_day):
            opened = datetime.combine(session_day, calendar.open_time, calendar.timezone)
            closed = datetime.combine(session_day, calendar.session_close_for(session_day), calendar.timezone)
            cursor = opened
            day_values: list[datetime] = []
            while cursor < closed:
                completed_at = min(cursor + step, closed)
                if completed_at <= local_reference:
                    day_values.append(cursor.astimezone(timezone.utc))
                cursor += step
            values = [*day_values, *values]
        session_day -= timedelta(days=1)
    return [_iso_utc_milliseconds(item) for item in values[-count:]]


def _days_in_month(day: date) -> int:
    return ((day.replace(day=28) + timedelta(days=4)).replace(day=1) - day).days


def _source_class(row: dict[str, Any], closed: bool) -> str:
    value = str(row.get("sourceClass") or row.get("source_class") or row.get("storageSource") or "").lower()
    if value in SOURCE_RANK_CURRENT: return value
    if row.get("isLive") or row.get("is_live"): return "active_live"
    if "redis" in value: return "redis_closed" if closed else "active_live"
    if "aggregate" in value or row.get("derived"): return "derived_aggregate"
    return "clickhouse_direct"


def _revision_epoch(row: dict[str, Any]) -> float:
    parsed = parse_utc_time(row.get("updatedAt") or row.get("updated_at") or row.get("createdAt") or row.get("created_at"))
    return parsed.timestamp() if parsed else 0.0


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decimal(value: Any) -> str:
    return format(Decimal(str(value or 0)).normalize(), "f")
