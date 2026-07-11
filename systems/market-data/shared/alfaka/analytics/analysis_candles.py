from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from alfaka.alpaca.feed_profiles import market_session_for_datetime
from alfaka.serving.time_utils import canonical_utc_timestamp, parse_utc_time


MARKET_TIMEZONE = ZoneInfo("America/New_York")
CANONICAL_DATA_VERSION = "v2"
SESSION_POLICY = "us-equity-regular"
ADJUSTMENT_POLICY = "split"
CANDLE_CONTRACT_VERSION = "analysis-candles-v1"
SOURCE_RANK_ANALYSIS = {"derived_aggregate": 1, "clickhouse_direct": 2}
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


def canonicalize_candle_identity(
    row: dict[str, Any], interval: str, session_policy: str = SESSION_POLICY
) -> dict[str, Any] | None:
    if session_policy != SESSION_POLICY or interval not in {"1D", "1W", "1M"}:
        raise ValueError("Unsupported analysis candle identity policy")
    parsed = parse_utc_time(row.get("timestamp") or row.get("event_time"))
    if parsed is None:
        return None
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
    timestamp = _market_midnight_utc(key_day)
    normalized = dict(row)
    normalized.update({"interval": interval, "candleKey": candle_key, "timestamp": timestamp})
    return normalized


def choose_canonical_winner(
    rows: Iterable[dict[str, Any]], *, view: str = "analysis_closed"
) -> dict[str, Any] | None:
    eligible = []
    ranks = SOURCE_RANK_ANALYSIS if view == "analysis_closed" else SOURCE_RANK_CURRENT
    for row in rows:
        if row.get("canonicalVersion", row.get("canonical_version", CANONICAL_DATA_VERSION)) != CANONICAL_DATA_VERSION:
            continue
        if row.get("priceAdjustment", row.get("price_adjustment", ADJUSTMENT_POLICY)) != ADJUSTMENT_POLICY:
            continue
        session = row.get("marketSession", row.get("market_session", "regular"))
        if session not in {None, "", "regular"}:
            continue
        is_closed = row.get("isClosed", row.get("is_closed", True)) is not False
        if view == "analysis_closed" and not is_closed:
            continue
        source = _source_class(row, is_closed)
        if source not in ranks:
            continue
        eligible.append((ranks[source], _revision_epoch(row), str(row.get("sourceEventId") or row.get("source_event_id") or ""), _stable_hash(row), row))
    if not eligible:
        return None
    return dict(max(eligible, key=lambda item: item[:4])[4])


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
    return sorted((row for row in winners if row), key=lambda row: (row["timestamp"], _stable_hash(row)))


def aggregate_analysis_candles(
    daily_rows: list[dict[str, Any]], interval: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    canonical = merge_canonical_candles(daily_rows, interval="1D", view="analysis_closed")
    if interval == "1D":
        return [_analysis_row(row, "1D", index) for index, row in enumerate(canonical)]
    if interval not in {"1W", "1M"}:
        raise ValueError(f"Unsupported analysis interval: {interval}")
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in canonical:
        identity = canonicalize_candle_identity(row, interval)
        if identity:
            buckets.setdefault(identity["candleKey"], []).append(identity)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: list[dict[str, Any]] = []
    for key in sorted(buckets):
        rows = sorted(buckets[key], key=lambda item: item["timestamp"])
        bucket_date = _bucket_date(key, interval)
        if _bucket_end(bucket_date, interval) > reference:
            continue
        aggregate = {
            **rows[0],
            "timestamp": _market_midnight_utc(bucket_date),
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
    rows: list[dict[str, Any]], interval: str, *, display_bars: int, now: datetime | None = None
) -> dict[str, Any]:
    reference = now or datetime.now(timezone.utc)
    actual = {str(row.get("candleKey")) for row in rows if row.get("candleKey")}
    if not rows:
        return _coverage_result([], actual, interval, reference)
    last_key = _last_expected_key(interval, reference)
    if last_key is None:
        expected: list[str] = []
    else:
        expected = _expected_keys_ending(interval, last_key, display_bars)
    return _coverage_result(expected, actual, interval, reference)


class AnalysisCandleSource:
    def __init__(self, provider: Any, *, now_provider=None):
        self.provider = provider
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def load_symbol(self, symbol: str, requested_intervals: Iterable[str]) -> AnalysisCandleBundle:
        intervals = tuple(dict.fromkeys(requested_intervals))
        if not intervals or set(intervals).difference({"1D", "1W", "1M"}):
            raise ValueError("Unsupported analysis intervals")
        from .schema import DISPLAY_BARS, LOOKBACK_BARS
        daily_limit = max(LOOKBACK_BARS[item] * {"1D": 1, "1W": 7, "1M": 31}[item] for item in intervals)
        raw = self.provider.daily_candles(symbol, interval="1D", limit=daily_limit)
        now = self.now_provider()
        rows = {item: aggregate_analysis_candles(raw, item, now=now)[-LOOKBACK_BARS[item]:] for item in intervals}
        coverage = {item: compute_analysis_coverage(rows[item], item, display_bars=DISPLAY_BARS[item], now=now) for item in intervals}
        digests = {item: analysis_input_digest(symbol, item, rows[item]) for item in intervals}
        return AnalysisCandleBundle(rows=rows, coverage=coverage, digests=digests)


def _analysis_row(row: dict[str, Any], interval: str, index: int) -> dict[str, Any]:
    try:
        values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        volume = float(row.get("volume") or 0)
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid canonical analysis candle") from None
    if not all(math.isfinite(value) for value in (*values.values(), volume)) or values["low"] <= 0 or values["high"] < values["low"]:
        raise ValueError("Invalid canonical analysis candle values")
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
    thresholds = {"1D": (0.95, 60, 2), "1W": (0.92, 26, 1), "1M": (0.90, 18, 1)}[interval]
    flags = []
    if ratio < thresholds[0]: flags.append("display_coverage_below_threshold")
    if contiguous < thresholds[1]: flags.append("recent_contiguous_below_threshold")
    if largest > thresholds[2]: flags.append("largest_gap_above_threshold")
    if last_expected and last_actual != last_expected: flags.append("stale_input")
    return {
        "expectedBars": len(expected), "actualBars": sum(hits), "missingBars": missing,
        "coverageRatio": round(ratio, 4), "recentContiguousBars": contiguous,
        "largestGapBars": largest, "lastExpectedClosedAt": last_expected,
        "lastActualClosedAt": last_actual, "renderable": not flags,
        "qualityFlags": flags,
    }


def _expected_keys_ending(interval: str, last_key: str, count: int) -> list[str]:
    cursor = _bucket_date(last_key, interval)
    result: list[str] = []
    while len(result) < count:
        if interval == "1D":
            at_open = datetime.combine(cursor, time(10), tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
            if market_session_for_datetime(at_open) != "closed": result.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        elif interval == "1W":
            if any(_is_session(cursor + timedelta(days=day)) for day in range(5)): result.append(cursor.isoformat())
            cursor -= timedelta(days=7)
        else:
            if any(_is_session(cursor + timedelta(days=day)) for day in range(_days_in_month(cursor))): result.append(cursor.strftime("%Y-%m"))
            cursor = (cursor - timedelta(days=1)).replace(day=1)
    return list(reversed(result))


def _last_expected_key(interval: str, now: datetime) -> str | None:
    local = now.astimezone(MARKET_TIMEZONE)
    day = local.date()
    if interval == "1D":
        if local.time() < time(16): day -= timedelta(days=1)
        while not _is_session(day): day -= timedelta(days=1)
        return day.isoformat()
    if interval == "1W":
        monday = day - timedelta(days=day.weekday())
        if local >= datetime.combine(monday + timedelta(days=7), time.min, tzinfo=MARKET_TIMEZONE): return monday.isoformat()
        prior = monday - timedelta(days=7)
        return prior.isoformat()
    month = day.replace(day=1)
    prior = (month - timedelta(days=1)).replace(day=1)
    return prior.strftime("%Y-%m")


def _is_session(day: date) -> bool:
    at_open = datetime.combine(day, time(10), tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
    return market_session_for_datetime(at_open) != "closed"


def _market_midnight_utc(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _bucket_date(key: str, interval: str) -> date:
    return date.fromisoformat(key + "-01") if interval == "1M" else date.fromisoformat(key)


def _bucket_end(day: date, interval: str) -> datetime:
    if interval == "1W": next_day = day + timedelta(days=7)
    else: next_day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return datetime.combine(next_day, time.min, tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)


def _key_timestamp(key: str, interval: str) -> str:
    return _market_midnight_utc(_bucket_date(key, interval))


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
