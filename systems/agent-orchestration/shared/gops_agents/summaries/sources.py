"""요약 재료를 저장소에서 읽는다. 외부 API 는 부르지 않고 ClickHouse·Redis 에 쌓인 값만 쓴다.

- 뉴스: ClickHouse news_article_localizations(한글) → 없으면 news_articles(원문)
- 일봉: ClickHouseMarketDataProvider.daily_candles
- 실적일: ClickHouse yahoo_earnings_estimates(sourceFrame=earnings_dates)
- 지수·히트맵: API 서버 워커가 Redis 에 써 둔 스냅샷(gops:market:on-demand:v1:indices:*, heatmap:sp500*)
- 캘린더(주간 요약): Postgres economic_events + ClickHouse yahoo_earnings_estimates(D28 과 같은 항목)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_MARKET_REDIS_PREFIX = "gops:market:on-demand:v1"


class SummarySource(Protocol):
    def news_symbols_since(self, hours: int) -> list[str]: ...
    def latest_news(self, symbol: str, limit: int) -> list[dict[str, Any]]: ...
    def daily_candles(self, symbol: str, limit: int) -> list[dict[str, Any]]: ...
    def upcoming_events(self, symbol: str) -> list[dict[str, Any]]: ...
    def heatmap_items(self) -> list[dict[str, Any]]: ...
    def index_items(self) -> list[dict[str, Any]]: ...
    def top_news(self, hours: int, limit: int) -> list[dict[str, Any]]: ...
    def calendar_items(self, from_date: date, to_date: date) -> list[dict[str, Any]]: ...


class StoreSummarySource:
    def __init__(self, *, clickhouse: Any | None = None, redis_client: Any | None = None) -> None:
        if clickhouse is None:
            from market_data.serving.clickhouse_provider import ClickHouseMarketDataProvider

            clickhouse = ClickHouseMarketDataProvider()
        self.clickhouse = clickhouse
        self.redis = redis_client

    # ---------- ClickHouse ----------

    def _query(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return list(self.clickhouse.query_json_each_row(query, params) or [])
        except Exception as exc:
            logger.warning("summary source query failed: %s", exc)
            return []

    def news_symbols_since(self, hours: int) -> list[str]:
        rows = self._query(
            f"""
            SELECT target_symbol AS symbol, count() AS articles
            FROM {self.clickhouse.table('news_article_localizations')}
            WHERE published_at >= now64(3) - INTERVAL {{hours:UInt32}} HOUR
              AND subject_relevance IN ('primary', 'secondary')
            GROUP BY target_symbol
            ORDER BY articles DESC
            FORMAT JSONEachRow
            """,
            {"hours": int(hours)},
        )
        if not rows:
            rows = self._query(
                f"""
                SELECT symbol, count() AS articles
                FROM {self.clickhouse.table('news_articles')}
                WHERE published_at >= now64(3) - INTERVAL {{hours:UInt32}} HOUR
                GROUP BY symbol
                ORDER BY articles DESC
                FORMAT JSONEachRow
                """,
                {"hours": int(hours)},
            )
        return [str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")]

    def latest_news(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            rows = list(self.clickhouse.localized_news_articles(symbol, limit=limit, days=7) or [])
        except Exception as exc:
            logger.warning("localized news read failed symbol=%s: %s", symbol, exc)
        if not rows:
            try:
                rows = list(self.clickhouse.news_articles(symbol, limit=limit, days=7) or [])
            except Exception as exc:
                logger.warning("news read failed symbol=%s: %s", symbol, exc)
        rows.sort(key=lambda row: str(row.get("publishedAt") or ""), reverse=True)
        return [news_row(row) for row in rows[:limit]]

    def top_news(self, hours: int, limit: int) -> list[dict[str, Any]]:
        rows = self._query(
            f"""
            SELECT
              article_id AS articleId,
              any(localized_headline) AS localizedHeadline,
              any(localized_summary) AS localizedSummary,
              groupUniqArray(target_symbol) AS symbols,
              max(relevance_score_v2) AS score,
              formatDateTime(max(published_at), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS publishedAt
            FROM {self.clickhouse.table('news_article_localizations')}
            WHERE published_at >= now64(3) - INTERVAL {{hours:UInt32}} HOUR
              AND subject_relevance IN ('primary', 'secondary')
            GROUP BY article_id
            ORDER BY length(symbols) DESC, score DESC, publishedAt DESC
            LIMIT {{limit:UInt32}}
            FORMAT JSONEachRow
            """,
            {"hours": int(hours), "limit": int(limit)},
        )
        return [news_row(row) for row in rows]

    def daily_candles(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        try:
            rows = list(self.clickhouse.daily_candles(symbol, "1D", limit=limit) or [])
        except Exception as exc:
            logger.warning("daily candle read failed symbol=%s: %s", symbol, exc)
            return []
        return [row for row in rows if row.get("isClosed") is not False]

    def upcoming_events(self, symbol: str) -> list[dict[str, Any]]:
        rows = self._query(
            f"""
            SELECT
              formatDateTime(event_at, '%Y-%m-%d', 'America/New_York') AS at,
              argMax(average, collected_at) AS epsEstimate
            FROM {self.clickhouse.table('yahoo_earnings_estimates')}
            WHERE symbol = {{symbol:String}}
              AND JSONExtractString(raw, 'sourceFrame') = 'earnings_dates'
              AND event_at IS NOT NULL
              AND event_at >= now64(3) - INTERVAL 12 HOUR
            GROUP BY at
            ORDER BY at ASC
            LIMIT 2
            FORMAT JSONEachRow
            """,
            {"symbol": symbol},
        )
        events = []
        for row in rows:
            estimate = _float(row.get("epsEstimate"))
            note = f"시장 예상 EPS {estimate:.2f}달러" if estimate is not None else ""
            events.append({"title": "실적 발표", "at": str(row.get("at") or ""), "note": note})
        return [event for event in events if event["at"]]

    def calendar_items(self, from_date: date, to_date: date) -> list[dict[str, Any]]:
        """한국 날짜 from~to 의 D28 항목. 실적은 시총 상위 50만 major 로 표시한다(로그인 사용자 구분 없음)."""
        from market_data.economic_calendar.items import build_earnings_items, econ_item
        from market_data.economic_calendar.store import postgres_conninfo, select_events

        from .calendar_brief import seoul_day_utc_bounds
        from .names import display_name

        items: list[dict[str, Any]] = []
        conninfo = postgres_conninfo()
        if conninfo:
            try:
                items.extend(econ_item(row) for row in select_events(conninfo, from_date, to_date))
            except Exception as exc:
                logger.warning("economic events read failed: %s", exc)
        start, end = seoul_day_utc_bounds(from_date, to_date)
        try:
            rows = list(self.clickhouse.earnings_events_between(start, end) or [])
        except Exception as exc:
            logger.warning("calendar earnings read failed: %s", exc)
            rows = []
        major = set(top_market_cap(self.heatmap_items(), 50) or seed_market_cap_symbols(50))
        items.extend(
            build_earnings_items(
                rows,
                name_ko=lambda symbol: next((name for name in [display_name(symbol)] if name != symbol), None),
                major=major,
                universe=major,
                from_date=from_date,
                to_date=to_date,
            )
        )
        return items

    # ---------- Redis 스냅샷 ----------

    def _redis_json(self, *keys: str) -> dict[str, Any] | None:
        if self.redis is None:
            return None
        for key in keys:
            try:
                raw = self.redis.get(key)
            except Exception as exc:
                logger.warning("redis read failed key=%s: %s", key, exc)
                continue
            if not raw:
                continue
            try:
                payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def heatmap_items(self) -> list[dict[str, Any]]:
        universe = os.getenv("HEATMAP_UNIVERSE", "sp500")
        payload = self._redis_json(market_redis_key(f"heatmap:{universe}"), market_redis_key(f"heatmap:{universe}:last"))
        items = (payload or {}).get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def index_items(self) -> list[dict[str, Any]]:
        payload = self._redis_json(market_redis_key("indices:snapshot"), market_redis_key("indices:last"))
        items = (payload or {}).get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def news_row(row: dict[str, Any]) -> dict[str, Any]:
    concerns = row.get("concerns")
    if isinstance(concerns, str):
        try:
            concerns = json.loads(concerns)
        except ValueError:
            concerns = [concerns]
    symbols = row.get("symbols")
    return {
        "newsId": str(row.get("articleId") or row.get("article_id") or ""),
        "title": row.get("localizedHeadline") or row.get("headline") or row.get("title") or "",
        "summary": row.get("localizedSummary") or row.get("summary") or "",
        "publishedAt": row.get("publishedAt"),
        "impactDirection": row.get("impactDirection"),
        "concerns": concerns if isinstance(concerns, list) else [],
        "symbols": symbols if isinstance(symbols, list) else [row.get("targetSymbol") or row.get("symbol")],
    }


def market_redis_key(value: str) -> str:
    prefix = (os.getenv("REDIS_KEY_PREFIX") or DEFAULT_MARKET_REDIS_PREFIX).strip().strip(":")
    return f"{prefix}:{value}" if prefix else value


def seed_market_cap_symbols(limit: int, path: Path | None = None) -> list[str]:
    """히트맵 스냅샷이 없을 때 쓰는 시총 순서(배포 이미지에 같이 들어 있는 시드)."""
    seed = path or _repo_root() / "systems" / "market-data" / "config" / "sp500-heatmap-seed.json"
    try:
        items = json.loads(seed.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError):
        return []
    return top_market_cap(items, limit)


def top_market_cap(items: list[dict[str, Any]], limit: int) -> list[str]:
    ranked = sorted(
        (item for item in items if isinstance(item, dict) and item.get("symbol")),
        key=lambda item: _float(item.get("marketCap")) or 0.0,
        reverse=True,
    )
    return [str(item["symbol"]).upper() for item in ranked[:limit]]


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "systems").is_dir():
            return parent
    return current.parents[-1]


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
