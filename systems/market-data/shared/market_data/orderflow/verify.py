from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from market_data.orderflow.classification import (
    classify_trade_side,
    merge_trades_with_quotes,
    normalize_quotes,
    normalize_trades,
    row_time,
)
from market_data.orderflow.config import price_bin_size_from_env, quote_future_tolerance_ms_from_env, quote_max_age_ms_from_env
from market_data.orderflow.rollup import (
    CountingIterator,
    MARKET_TIMEZONE,
    RecentIdDeduper,
    dedupe_rows,
    hourly_windows,
    iso,
    normalize_quote_tick_row,
    normalize_trade_tick_row,
    query_quote_rows,
    query_trade_rows,
    regular_session_bounds_utc,
)


SIDES = ("ask", "bid", "unknown")
MARKET_SKEW_THRESHOLD = 0.30
DELTA_SKEW_THRESHOLD = 0.15
SIGN_MISMATCH_THRESHOLD = 0.25
UNKNOWN_RATIO_THRESHOLD = 0.15


def build_order_flow_verification_report(
    client,
    symbol: str,
    session_date: str | date,
    *,
    price_bin_size: float | None = None,
    live_payload: dict[str, Any] | None = None,
    fetch_live: bool = True,
    api_base_url: str | None = None,
    include_minutes: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    session_day = parse_session_date(session_date)
    price_step = float(price_bin_size if price_bin_size is not None else price_bin_size_from_env())
    asof = asof_ticks_profile(client, symbol, session_day, price_bin_size=price_step)
    daily = daily_row_profile(client, symbol, session_day)
    live = live_intraday_profile(
        symbol,
        session_day,
        live_payload=live_payload,
        fetch_live=fetch_live,
        api_base_url=api_base_url,
        now=now,
    )
    report = {
        "symbol": symbol,
        "sessionDate": session_day.isoformat(),
        "sources": {
            "live": compact_source(live, include_minutes=include_minutes),
            "asofTicks": compact_source(asof, include_minutes=include_minutes),
            "dailyRow": compact_source(daily, include_minutes=include_minutes),
        },
        "comparisons": {
            "liveVsAsofTicks": compare_minute_profiles(live, asof),
            "dailyRowVsAsofTicks": compare_total_profiles(daily, asof),
        },
        "quoteCoverage": asof.get("quoteCoverage", {}),
    }
    report["verdict"] = classify_verdict(report)
    return report


def asof_ticks_profile(client, symbol: str, session_day: date, *, price_bin_size: float) -> dict[str, Any]:
    bounds = regular_session_bounds_utc(session_day)
    if bounds is None:
        return empty_source("asof-ticks", symbol, session_day, status="closed")
    session_start, session_end, quote_warmup_start = bounds
    minute_levels: dict[str, dict[float, dict[str, Any]]] = defaultdict(dict)
    quote_counts: Counter[str] = Counter()
    trade_deduper = RecentIdDeduper()
    quote_deduper = RecentIdDeduper()
    duplicate_count = 0
    quote_count = 0
    trade_count = 0
    carry_quote = None
    max_quote_age_ms = quote_max_age_ms_from_env()
    future_tolerance_ms = quote_future_tolerance_ms_from_env()

    for win_start, win_end in hourly_windows(session_start, session_end):
        quote_start = quote_warmup_start if win_start == session_start else win_start
        trade_rows = query_trade_rows(client, symbol, win_start, win_end)
        quote_rows = query_quote_rows(client, symbol, quote_start, win_end)
        trade_rows, trade_dupes = dedupe_rows(trade_rows, trade_deduper)
        quote_rows, quote_dupes = dedupe_rows(quote_rows, quote_deduper)
        duplicate_count += trade_dupes + quote_dupes
        trades = normalize_trades([normalize_trade_tick_row(row) for row in trade_rows])
        quotes = normalize_quotes([normalize_quote_tick_row(row) for row in quote_rows])
        for quote in quotes:
            quote_dt = row_time(quote)
            if quote_dt is not None and session_start <= quote_dt < session_end:
                quote_counts[minute_iso(quote_dt)] += 1
        quote_counter = CountingIterator(quotes)
        for trade, quote in merge_trades_with_quotes(trades, quote_counter, initial_quote=carry_quote):
            trade_count += 1
            side = classify_trade_side(
                trade,
                quote,
                max_quote_age_ms=max_quote_age_ms,
                future_tolerance_ms=future_tolerance_ms,
            )
            add_trade_to_minute_levels(minute_levels, trade, side, price_bin_size)
        for _quote in quote_counter:
            pass
        quote_count += quote_counter.count
        carry_quote = quote_counter.last or carry_quote

    minutes = minutes_from_levels(minute_levels)
    source = source_from_minutes("asof-ticks", symbol, session_day, minutes)
    source.update({
        "tradeCount": trade_count,
        "quoteCount": quote_count,
        "duplicateCount": duplicate_count,
        "quoteCoverage": quote_coverage_summary(session_start, session_end, quote_counts),
    })
    return source


def daily_row_profile(client, symbol: str, session_day: date) -> dict[str, Any]:
    query = """
    SELECT
      toString(session_date) AS sessionDate,
      price_bin AS priceBin,
      price_bin_size AS priceBinSize,
      ask_volume AS askVolume,
      bid_volume AS bidVolume,
      unknown_volume AS unknownVolume,
      ask_trade_count AS askTradeCount,
      bid_trade_count AS bidTradeCount,
      unknown_trade_count AS unknownTradeCount,
      trade_count AS tradeCount,
      volume
    FROM market_data.order_flow_profile_daily_latest
    WHERE symbol = {symbol:String}
      AND session_date = toDate({sessionDate:String})
    ORDER BY price_bin ASC
    FORMAT JSONEachRow
    """
    rows = client.query_json_each_row(query, {"symbol": symbol, "sessionDate": session_day.isoformat()})
    if not rows:
        return empty_source("daily-row", symbol, session_day)
    source = source_from_levels("daily-row", symbol, session_day, rows)
    source["rowCount"] = len(rows)
    return source


def live_intraday_profile(
    symbol: str,
    session_day: date,
    *,
    live_payload: dict[str, Any] | None = None,
    fetch_live: bool = True,
    api_base_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if live_payload is None:
        if not fetch_live:
            return empty_source("live", symbol, session_day, status="skipped")
        if session_day != current_market_date(now):
            return empty_source("live", symbol, session_day, status="omitted-past-session")
        try:
            live_payload = fetch_live_intraday_payload(symbol, api_base_url=api_base_url)
        except Exception as exc:
            source = empty_source("live", symbol, session_day, status="unavailable")
            source["error"] = str(exc)
            return source
    payload_status = str(live_payload.get("dataStatus") or "empty")
    payload_session = str(live_payload.get("sessionDate") or session_day.isoformat())
    if payload_session != session_day.isoformat():
        source = empty_source("live", symbol, session_day, status="stale-session")
        source["payloadSessionDate"] = payload_session
        return source
    minutes = [
        {
            "eventMinute": str(minute.get("eventMinute") or ""),
            "bins": [dict(level) for level in minute.get("bins") or [] if isinstance(level, dict)],
        }
        for minute in live_payload.get("minutes") or []
        if isinstance(minute, dict)
    ]
    source = source_from_minutes("live", symbol, session_day, minutes, status=payload_status)
    source["payloadStatus"] = payload_status
    return source


def fetch_live_intraday_payload(symbol: str, *, api_base_url: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    base = (api_base_url or os.getenv("ORDER_FLOW_VERIFY_API_BASE_URL") or "http://localhost:8000").rstrip("/")
    url = f"{base}/api/charts/order-flow/intraday?{urlencode({'symbol': symbol})}"
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def source_from_minutes(
    source: str,
    symbol: str,
    session_day: date,
    minutes: list[dict[str, Any]],
    *,
    status: str | None = None,
) -> dict[str, Any]:
    minute_items = []
    all_levels = []
    for minute in sorted(minutes, key=lambda item: str(item.get("eventMinute") or "")):
        levels = [dict(level) for level in minute.get("bins") or [] if isinstance(level, dict)]
        all_levels.extend(levels)
        minute_items.append({
            "eventMinute": minute.get("eventMinute"),
            "metrics": metrics_from_levels(levels),
        })
    metrics = metrics_from_levels(all_levels)
    return {
        "source": source,
        "symbol": symbol,
        "sessionDate": session_day.isoformat(),
        "status": status or ("ready" if metrics["totalVolume"] > 0 else "empty"),
        "metrics": metrics,
        "minutes": minute_items,
    }


def source_from_levels(source: str, symbol: str, session_day: date, levels: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = metrics_from_levels(levels)
    return {
        "source": source,
        "symbol": symbol,
        "sessionDate": session_day.isoformat(),
        "status": "ready" if metrics["totalVolume"] > 0 else "empty",
        "metrics": metrics,
        "minutes": [],
    }


def compact_source(source: dict[str, Any], *, include_minutes: bool) -> dict[str, Any]:
    output = {key: value for key, value in source.items() if key not in {"minutes", "quoteCoverage"}}
    if include_minutes:
        output["minutes"] = source.get("minutes", [])
    return output


def compare_minute_profiles(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    total = compare_total_profiles(left, right)
    left_minutes = {str(item.get("eventMinute")): item for item in left.get("minutes", [])}
    right_minutes = {str(item.get("eventMinute")): item for item in right.get("minutes", [])}
    common = sorted(set(left_minutes) & set(right_minutes))
    left_skews = [float(left_minutes[key].get("metrics", {}).get("skew") or 0.0) for key in common]
    right_skews = [float(right_minutes[key].get("metrics", {}).get("skew") or 0.0) for key in common]
    mismatches = sum(1 for left_skew, right_skew in zip(left_skews, right_skews) if sign(left_skew) != sign(right_skew))
    total.update({
        "commonMinuteCount": len(common),
        "skewCorrelation": pearson(left_skews, right_skews),
        "signMismatchRate": mismatches / len(common) if common else None,
    })
    return total


def compare_total_profiles(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_metrics = left.get("metrics", {})
    right_metrics = right.get("metrics", {})
    left_skew = float(left_metrics.get("skew") or 0.0)
    right_skew = float(right_metrics.get("skew") or 0.0)
    return {
        "leftStatus": left.get("status"),
        "rightStatus": right.get("status"),
        "deltaSkew": abs(left_skew - right_skew),
        "leftSkew": left_skew,
        "rightSkew": right_skew,
        "leftUnknownRatio": float(left_metrics.get("unknownRatio") or 0.0),
        "rightUnknownRatio": float(right_metrics.get("unknownRatio") or 0.0),
    }


def classify_verdict(report: dict[str, Any]) -> dict[str, Any]:
    sources = report.get("sources", {})
    live = sources.get("live", {})
    asof = sources.get("asofTicks", {})
    daily = sources.get("dailyRow", {})
    live_vs_asof = report.get("comparisons", {}).get("liveVsAsofTicks", {})
    findings = []
    max_unknown = max(
        float(live.get("metrics", {}).get("unknownRatio") or 0.0),
        float(asof.get("metrics", {}).get("unknownRatio") or 0.0),
    )
    if max_unknown > UNKNOWN_RATIO_THRESHOLD:
        findings.append({
            "rule": "C",
            "title": "quote coverage issue",
            "reason": f"unknownRatio {max_unknown:.4f} exceeds {UNKNOWN_RATIO_THRESHOLD:.2f}",
        })
    if live_vs_asof.get("leftStatus") == "ready" and live_vs_asof.get("rightStatus") == "ready":
        delta_skew = float(live_vs_asof.get("deltaSkew") or 0.0)
        sign_mismatch = live_vs_asof.get("signMismatchRate")
        if delta_skew > DELTA_SKEW_THRESHOLD or (
            sign_mismatch is not None and sign_mismatch > SIGN_MISMATCH_THRESHOLD
        ):
            findings.append({
                "rule": "B",
                "title": "live classification artifact candidate",
                "reason": (
                    f"deltaSkew={delta_skew:.4f}, "
                    f"signMismatchRate={sign_mismatch if sign_mismatch is not None else 'n/a'}"
                ),
            })
        else:
            asof_skew = float(asof.get("metrics", {}).get("skew") or 0.0)
            daily_skew = float(daily.get("metrics", {}).get("skew") or 0.0)
            daily_matches = daily.get("status") != "ready" or sign(daily_skew) == sign(asof_skew)
            if abs(asof_skew) > MARKET_SKEW_THRESHOLD and daily_matches:
                findings.append({
                    "rule": "A",
                    "title": "market skew likely",
                    "reason": f"asof skew {asof_skew:.4f} is large and live is similar",
                })
    if not findings:
        findings.append({
            "rule": "INCONCLUSIVE",
            "title": "needs more sessions",
            "reason": "thresholds A/B/C were not triggered",
        })
    return {
        "primary": findings[0]["rule"],
        "findings": findings,
        "thresholds": {
            "marketSkew": MARKET_SKEW_THRESHOLD,
            "deltaSkew": DELTA_SKEW_THRESHOLD,
            "signMismatchRate": SIGN_MISMATCH_THRESHOLD,
            "unknownRatio": UNKNOWN_RATIO_THRESHOLD,
        },
    }


def metrics_from_levels(levels: list[dict[str, Any]]) -> dict[str, float]:
    ask = sum(number(level.get("askVolume", level.get("ask_volume"))) for level in levels)
    bid = sum(number(level.get("bidVolume", level.get("bid_volume"))) for level in levels)
    unknown = sum(number(level.get("unknownVolume", level.get("unknown_volume"))) for level in levels)
    total = ask + bid + unknown
    side_total = max(1.0, ask + bid)
    return {
        "totalVolume": total,
        "askVolume": ask,
        "bidVolume": bid,
        "unknownVolume": unknown,
        "unknownRatio": unknown / total if total > 0 else 0.0,
        "skew": (ask - bid) / side_total,
    }


def add_trade_to_minute_levels(
    minute_levels: dict[str, dict[float, dict[str, Any]]],
    trade: dict[str, Any],
    side: str,
    price_bin_size: float,
) -> None:
    trade_dt = row_time(trade)
    if trade_dt is None:
        return
    price = number(trade.get("price"))
    size = number(trade.get("size"))
    if price <= 0 or size <= 0:
        return
    event_minute = minute_iso(trade_dt)
    price_bin = round(round(price / price_bin_size) * price_bin_size, 6)
    level = minute_levels[event_minute].setdefault(price_bin, new_level(price_bin))
    volume_key, count_key = side_keys(side)
    level[volume_key] += size
    level[count_key] += 1


def minutes_from_levels(minute_levels: dict[str, dict[float, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "eventMinute": event_minute,
            "bins": [level for _price, level in sorted(levels.items())],
        }
        for event_minute, levels in sorted(minute_levels.items())
    ]


def quote_coverage_summary(session_start: datetime, session_end: datetime, quote_counts: Counter[str]) -> dict[str, Any]:
    expected = []
    cursor = session_start.replace(second=0, microsecond=0)
    while cursor < session_end:
        expected.append(minute_iso(cursor))
        cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=1)
    counts = [quote_counts.get(minute, 0) for minute in expected]
    distribution = Counter(counts)
    return {
        "regularMinuteCount": len(expected),
        "quoteGapMinuteCount": sum(1 for count in counts if count == 0),
        "quoteCountDistribution": {str(key): distribution[key] for key in sorted(distribution)},
        "minQuotesPerMinute": min(counts) if counts else 0,
        "maxQuotesPerMinute": max(counts) if counts else 0,
    }


def print_human_report(report: dict[str, Any]) -> None:
    print(f"Order-flow verification {report['symbol']} {report['sessionDate']}")
    for name, source in report["sources"].items():
        metrics = source.get("metrics", {})
        print(
            f"- {name}: status={source.get('status')} total={metrics.get('totalVolume', 0):.2f} "
            f"ask={metrics.get('askVolume', 0):.2f} bid={metrics.get('bidVolume', 0):.2f} "
            f"unknown={metrics.get('unknownVolume', 0):.2f} "
            f"unknownRatio={metrics.get('unknownRatio', 0):.4f} skew={metrics.get('skew', 0):.4f}"
        )
    live_cmp = report["comparisons"]["liveVsAsofTicks"]
    print(
        "- live vs asof: "
        f"deltaSkew={live_cmp.get('deltaSkew')} "
        f"corr={live_cmp.get('skewCorrelation')} "
        f"signMismatchRate={live_cmp.get('signMismatchRate')}"
    )
    coverage = report.get("quoteCoverage", {})
    print(
        "- quote coverage: "
        f"gapMinutes={coverage.get('quoteGapMinuteCount', 0)}/"
        f"{coverage.get('regularMinuteCount', 0)} distribution={coverage.get('quoteCountDistribution', {})}"
    )
    verdict = report["verdict"]
    print(f"- verdict: {verdict['primary']}")
    for finding in verdict["findings"]:
        print(f"  {finding['rule']}: {finding['title']} - {finding['reason']}")


def empty_source(source: str, symbol: str, session_day: date, *, status: str = "empty") -> dict[str, Any]:
    return {
        "source": source,
        "symbol": symbol,
        "sessionDate": session_day.isoformat(),
        "status": status,
        "metrics": metrics_from_levels([]),
        "minutes": [],
    }


def new_level(price_bin: float) -> dict[str, Any]:
    return {
        "priceBin": price_bin,
        "askVolume": 0.0,
        "bidVolume": 0.0,
        "unknownVolume": 0.0,
        "askTradeCount": 0,
        "bidTradeCount": 0,
        "unknownTradeCount": 0,
    }


def side_keys(side: str) -> tuple[str, str]:
    if side == "ask":
        return "askVolume", "askTradeCount"
    if side == "bid":
        return "bidVolume", "bidTradeCount"
    return "unknownVolume", "unknownTradeCount"


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_den = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_den = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    denominator = left_den * right_den
    return numerator / denominator if denominator > 0 else None


def sign(value: float) -> int:
    if value > 1e-9:
        return 1
    if value < -1e-9:
        return -1
    return 0


def minute_iso(value: datetime) -> str:
    return iso(value.astimezone(timezone.utc).replace(second=0, microsecond=0))


def current_market_date(now: datetime | None = None) -> date:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(MARKET_TIMEZONE).date()


def parse_session_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return 0.0
        return parsed if math.isfinite(parsed) else 0.0
    return 0.0
