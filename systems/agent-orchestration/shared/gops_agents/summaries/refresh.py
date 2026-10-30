"""종목 AI 요약·오늘 시장 요약 갱신 잡의 본체(jobs/stock-summary-refresh/main.py 가 부른다).

대상 종목: 최근 24시간 뉴스가 있는 종목 + 시총 상위 50. 종목마다 1시간에 한 번만 다시 만든다
(Redis 에 저장된 generatedAt 기준). 결과는 gops:stocks:ai-summary:{SYMBOL}, TTL 24시간.
시장 요약은 미국 장 마감(16:15 ET, 조기 폐장일은 13:15 ET) 뒤 첫 실행에서 한 번 만든다.
캘린더 주간 요약은 이번 주·다음 주(한국 시간 ISO 주)를 6시간에 한 번씩 다시 만든다. gops:calendar-brief:{week}, TTL 8일.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .calendar_brief import CalendarBriefGenerator, CalendarBriefInput, current_and_next_week, week_bounds
from .keys import (
    CALENDAR_BRIEF_TTL_SECONDS,
    MARKET_BRIEF_TTL_SECONDS,
    STOCK_SUMMARY_TTL_SECONDS,
    calendar_brief_key,
    market_brief_key,
    stock_summary_key,
)
from .market_brief import MarketBriefGenerator, MarketBriefInput
from .names import display_name
from .sources import SummarySource, seed_market_cap_symbols, top_market_cap
from .stock_summary import StockSummaryGenerator, StockSummaryInput
from .text_rules import finite, parse_iso

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
CLOSE_BUFFER = timedelta(minutes=15)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


@dataclass(frozen=True)
class RefreshConfig:
    news_hours: int = 24
    top_market_cap: int = 50
    news_limit: int = 8
    candle_limit: int = 20
    min_interval_seconds: int = 3600
    max_symbols: int = 300
    brief_news_limit: int = 6
    calendar_brief_min_interval_seconds: int = 6 * 3600

    @classmethod
    def from_env(cls) -> "RefreshConfig":
        return cls(
            news_hours=_int_env("STOCK_SUMMARY_NEWS_HOURS", cls.news_hours),
            top_market_cap=_int_env("STOCK_SUMMARY_TOP_MARKET_CAP", cls.top_market_cap),
            news_limit=_int_env("STOCK_SUMMARY_NEWS_LIMIT", cls.news_limit),
            candle_limit=_int_env("STOCK_SUMMARY_CANDLE_LIMIT", cls.candle_limit),
            min_interval_seconds=_int_env("STOCK_SUMMARY_MIN_INTERVAL_SECONDS", cls.min_interval_seconds),
            max_symbols=_int_env("STOCK_SUMMARY_MAX_SYMBOLS", cls.max_symbols),
            calendar_brief_min_interval_seconds=_int_env(
                "CALENDAR_BRIEF_MIN_INTERVAL_SECONDS", cls.calendar_brief_min_interval_seconds
            ),
        )


# ---------- 종목 요약 ----------

def select_symbols(source: SummarySource, config: RefreshConfig, heatmap_items: list[dict[str, Any]] | None = None) -> list[str]:
    """뉴스가 있는 종목을 먼저, 그다음 시총 상위. 중복 없이 max_symbols 까지."""
    items = heatmap_items if heatmap_items is not None else source.heatmap_items()
    by_cap = top_market_cap(items, config.top_market_cap) or seed_market_cap_symbols(config.top_market_cap)
    ordered = [*source.news_symbols_since(config.news_hours), *by_cap]
    symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in ordered if symbol and symbol.strip()))
    return symbols[: config.max_symbols]


def recently_generated(redis_client: Any, symbol: str, *, now: datetime, min_interval_seconds: int) -> bool:
    payload = _read_json(redis_client, stock_summary_key(symbol))
    generated_at = parse_iso((payload or {}).get("generatedAt"))
    return generated_at is not None and (now - generated_at).total_seconds() < min_interval_seconds


def sector_context(heatmap_items: list[dict[str, Any]]) -> dict[str, tuple[str, float]]:
    """symbol -> (업종 한글명, 업종 시총가중 등락률)."""
    groups: dict[str, list[tuple[float, float]]] = {}
    sector_of: dict[str, str] = {}
    for item in heatmap_items:
        symbol = str(item.get("symbol") or "").upper()
        sector = item.get("sectorLabelKo") or item.get("sector")
        change = finite(item.get("changePercent"))
        if not symbol or not sector:
            continue
        sector_of[symbol] = sector
        if change is not None:
            groups.setdefault(sector, []).append((change, finite(item.get("marketCap")) or 0.0))
    averages = {}
    for sector, rows in groups.items():
        total = sum(cap for _, cap in rows)
        averages[sector] = sum(c * cap for c, cap in rows) / total if total > 0 else sum(c for c, _ in rows) / len(rows)
    return {symbol: (sector, round(averages[sector], 2)) for symbol, sector in sector_of.items() if sector in averages}


def refresh_stock_summaries(
    source: SummarySource,
    redis_client: Any,
    generator: StockSummaryGenerator,
    *,
    config: RefreshConfig,
    now: datetime,
    symbols: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    heatmap_items = source.heatmap_items()
    targets = symbols or select_symbols(source, config, heatmap_items)
    sectors = sector_context(heatmap_items)
    stats: dict[str, Any] = {"selected": len(targets), "written": 0, "llm": 0, "template": 0, "throttled": 0, "noData": 0, "failed": 0}
    for symbol in targets:
        if not force and recently_generated(redis_client, symbol, now=now, min_interval_seconds=config.min_interval_seconds):
            stats["throttled"] += 1
            continue
        try:
            sector = sectors.get(symbol)
            data = StockSummaryInput(
                symbol=symbol,
                news=source.latest_news(symbol, config.news_limit),
                candles=source.daily_candles(symbol, config.candle_limit),
                events=source.upcoming_events(symbol),
                name_ko=display_name(symbol),
                sector_label_ko=sector[0] if sector else None,
                sector_change_percent=sector[1] if sector else None,
            )
            if not data.has_data():
                stats["noData"] += 1
                continue
            summary, used = generator.generate_with_source(data)
            _write_json(redis_client, stock_summary_key(symbol), summary, STOCK_SUMMARY_TTL_SECONDS)
            stats["written"] += 1
            stats[used] += 1
        except Exception as exc:  # 한 종목 실패가 잡 전체를 멈추지 않게 한다.
            logger.exception("stock summary failed symbol=%s", symbol)
            stats["failed"] += 1
            stats.setdefault("errors", []).append({"symbol": symbol, "error": type(exc).__name__})
    return stats


# ---------- 시장 요약 ----------

def session_close(day: date) -> datetime:
    from market_data.common.trading_calendar import is_us_equity_early_close_date

    close = EARLY_CLOSE if is_us_equity_early_close_date(day) else REGULAR_CLOSE
    return datetime.combine(day, close, tzinfo=NEW_YORK) + CLOSE_BUFFER


def is_session(day: date) -> bool:
    from market_data.common.trading_calendar import configured_closed_dates, is_us_equity_session_date

    try:
        closed = configured_closed_dates(day.year, day.year)
    except TypeError:
        closed = None
    return is_us_equity_session_date(day, configured_dates=closed)


def latest_closed_session(now: datetime) -> date:
    day = now.astimezone(NEW_YORK).date()
    if is_session(day) and now >= session_close(day):
        return day
    day -= timedelta(days=1)
    for _ in range(10):
        if is_session(day):
            return day
        day -= timedelta(days=1)
    return day


def brief_market_date(now: datetime, *, force: bool) -> str | None:
    """정기 실행은 '오늘 장이 막 끝났을 때'만 날짜를 준다. --brief 는 가장 최근 마감일을 준다."""
    session = latest_closed_session(now)
    if force or session == now.astimezone(NEW_YORK).date():
        return session.isoformat()
    return None


class MarketBriefArchive:
    """S3 에 market-brief/v1/date=YYYY-MM-DD/brief.json 으로 남긴다. 버킷이 없으면 꺼진다."""

    def __init__(self, *, client: Any | None = None, bucket: str | None = None, prefix: str | None = None) -> None:
        self.client = client
        self.bucket = bucket if bucket is not None else (os.getenv("MARKET_BRIEF_S3_BUCKET") or "").strip()
        self.prefix = (prefix or os.getenv("MARKET_BRIEF_S3_PREFIX") or "market-brief/v1").strip("/")

    def enabled(self) -> bool:
        return bool(self.bucket)

    def key(self, market_date: str) -> str:
        return f"{self.prefix}/date={market_date}/brief.json"

    def put(self, brief: dict[str, Any]) -> str | None:
        if not self.enabled():
            return None
        if self.client is None:
            import boto3

            self.client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
        key = self.key(brief["marketDate"])
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(brief, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return f"s3://{self.bucket}/{key}"


def refresh_market_brief(
    source: SummarySource,
    redis_client: Any,
    generator: MarketBriefGenerator,
    *,
    now: datetime,
    force: bool = False,
    archive: MarketBriefArchive | None = None,
    config: RefreshConfig | None = None,
) -> dict[str, Any]:
    config = config or RefreshConfig()
    market_date = brief_market_date(now, force=force)
    if market_date is None:
        return {"status": "skipped", "reason": "market_not_closed_today"}
    key = market_brief_key(market_date)
    if not force and _read_json(redis_client, key):
        return {"status": "skipped", "reason": "already_generated", "marketDate": market_date}
    data = MarketBriefInput(
        market_date=market_date,
        indices=source.index_items(),
        heatmap_items=source.heatmap_items(),
        news=source.top_news(config.news_hours, config.brief_news_limit),
    )
    if not data.has_data():
        return {"status": "skipped", "reason": "no_market_data", "marketDate": market_date}
    brief, used = generator.generate_with_source(data)
    if not brief["paragraphs"]:
        return {"status": "skipped", "reason": "empty_brief", "marketDate": market_date}
    _write_json(redis_client, key, brief, MARKET_BRIEF_TTL_SECONDS)
    result: dict[str, Any] = {"status": "written", "marketDate": market_date, "generator": used}
    archive = archive or MarketBriefArchive()
    try:
        location = archive.put(brief)
        if location:
            result["s3"] = location
    except Exception as exc:  # S3 는 보조 저장. Redis 에 쓴 값은 그대로 둔다.
        logger.warning("market brief s3 archive failed: %s", exc)
        result["s3Error"] = type(exc).__name__
    return result


# ---------- 캘린더 주간 요약 ----------

def refresh_calendar_briefs(
    source: SummarySource,
    redis_client: Any,
    generator: CalendarBriefGenerator,
    *,
    now: datetime,
    weeks: list[str] | None = None,
    force: bool = False,
    config: RefreshConfig | None = None,
) -> dict[str, Any]:
    """이번 주·다음 주 요약을 만든다. 저장된 generatedAt 이 6시간 안이면 건너뛴다(--calendar-brief 는 무시)."""
    config = config or RefreshConfig()
    results: dict[str, Any] = {}
    for week in weeks or current_and_next_week(now):
        key = calendar_brief_key(week)
        stored = _read_json(redis_client, key)
        generated_at = parse_iso((stored or {}).get("generatedAt"))
        if (
            not force
            and generated_at is not None
            and (now - generated_at).total_seconds() < config.calendar_brief_min_interval_seconds
        ):
            results[week] = {"status": "skipped", "reason": "recently_generated"}
            continue
        try:
            monday, sunday = week_bounds(week)
            data = CalendarBriefInput(week=week, items=source.calendar_items(monday, sunday))
            if not data.has_data():
                results[week] = {"status": "skipped", "reason": "no_events"}
                continue
            brief, used = generator.generate_with_source(data)
            _write_json(redis_client, key, brief, CALENDAR_BRIEF_TTL_SECONDS)
            results[week] = {"status": "written", "generator": used}
        except Exception as exc:  # 요약 실패가 다른 단계를 멈추지 않게 한다.
            logger.exception("calendar brief failed week=%s", week)
            results[week] = {"status": "failed", "error": type(exc).__name__}
    return results


# ---------- Redis 도우미 ----------

def _read_json(redis_client: Any, key: str) -> dict[str, Any] | None:
    try:
        raw = redis_client.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(redis_client: Any, key: str, payload: dict[str, Any], ttl: int) -> None:
    redis_client.set(key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), ex=ttl)


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
