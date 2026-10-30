"""투자 복기(D27)가 읽는 시장 데이터: 일봉 종가와 종목 뉴스 시각.

ClickHouse 일봉(`daily_candles`)과 종목 뉴스(`localized_news_articles_for_symbols_by_relevance`)
를 기존 provider 로 읽는다. 테스트는 같은 두 메서드를 가진 가짜 객체를
`app.state.review_market_data` 에 넣는다.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DAILY_CANDLE_LIMIT = 1000


class ReviewMarketData(Protocol):
    def daily_closes(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
        """start~end(양끝 포함) 거래일 종가를 날짜 오름차순으로."""

    def news_published_times(self, symbol: str, until: datetime, window: timedelta) -> list[datetime]:
        """until 이전 window 안에 나온 관련 뉴스의 발행 시각."""


class ClickHouseReviewMarketData:
    def __init__(self, clickhouse_provider: Any) -> None:
        self._clickhouse = clickhouse_provider

    def daily_closes(self, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
        reader = getattr(self._clickhouse, "daily_candles", None)
        if not callable(reader):
            return []
        from_time = datetime.combine(start, time.min, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        to_time = datetime.combine(end, time.max, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            rows = reader(symbol, "1D", DAILY_CANDLE_LIMIT, from_time=from_time, to_time=to_time) or []
        except Exception:
            logger.warning("review daily candle read failed: %s", symbol, exc_info=True)
            return []
        return closes_from_candles(rows)

    def news_published_times(self, symbol: str, until: datetime, window: timedelta) -> list[datetime]:
        reader = getattr(self._clickhouse, "localized_news_articles_for_symbols_by_relevance", None)
        if not callable(reader):
            return []
        try:
            rows = reader(
                [symbol],
                limit=10,
                days=max(1, window.days + 1),
                relevance_levels=["primary", "secondary"],
                as_of=until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            ) or []
        except Exception:
            logger.warning("review news read failed: %s", symbol, exc_info=True)
            return []
        times = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            published = parse_datetime(row.get("publishedAt") or row.get("published_at"))
            if published is not None:
                times.append(published)
        return times


def closes_from_candles(rows: list[Any]) -> list[tuple[date, float]]:
    """일봉 행에서 (거래일, 종가) 를 뽑는다. 거래일은 캔들 시각의 UTC 날짜다."""
    by_day: dict[date, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        at = parse_datetime(row.get("timestamp") or row.get("time") or row.get("date"))
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if at is None or close <= 0:
            continue
        by_day[at.astimezone(timezone.utc).date()] = close
    return sorted(by_day.items())


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def review_market_data_from_app(app: Any) -> ReviewMarketData:
    existing = getattr(app.state, "review_market_data", None)
    if existing is not None:
        return existing
    from app.services.alfaka_market_data import get_market_data_provider

    provider = get_market_data_provider()
    market = ClickHouseReviewMarketData(getattr(provider, "clickhouse_provider", None))
    app.state.review_market_data = market
    return market
