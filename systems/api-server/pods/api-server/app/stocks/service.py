"""홈 순위표(D5)와 종목 요약(D9) 계산.

데이터 출처를 모두 생성자 인자로 받는다. 테스트는 가짜 함수를 넣고,
운영은 build_stocks_service() 가 히트맵 projection·ClickHouse·Redis 를 연결한다.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from app.services.instrument_names import korean_name_fields, name_ko

logger = logging.getLogger(__name__)

MOVER_SORTS = ("trade", "volume", "up", "down")
DEFAULT_MOVERS_LIMIT = 20
MAX_MOVERS_LIMIT = 100
VOLUME_AVERAGE_DAYS = 20
MIN_VOLUME_AVERAGE_DAYS = 5
AVG_VOLUME_CACHE_TTL_SECONDS = 3600
AI_SUMMARY_KEY_PREFIX = "gops:stocks:ai-summary:"
AVG_VOLUME_KEY_PREFIX = "gops:stocks:avg-volume-20d:"
SUMMARY_KEY_PREFIX = "gops:stocks:summary:"
SUMMARY_CACHE_TTL_SECONDS = 60
WEEKS_52_TRADING_DAYS = 252
SECTOR_PER_PEER_LIMIT = 30
PEER_COUNT = 3

HeatmapProvider = Callable[[], dict[str, Any]]
DailyVolumesReader = Callable[[list[str], int], dict[str, list[float]]]
DailyCandlesReader = Callable[[str, int], list[dict[str, Any]]]
EpsReader = Callable[[list[str]], dict[str, float | None]]
LivePriceReader = Callable[[str], float | None]


class SymbolNotFound(LookupError):
    """히트맵 projection 에도 일봉에도 없는 종목."""

_stocks_service: "StocksService | None" = None


class StocksService:
    def __init__(
        self,
        *,
        heatmap_provider: HeatmapProvider,
        daily_volumes_reader: DailyVolumesReader | None = None,
        daily_candles_reader: DailyCandlesReader | None = None,
        eps_reader: EpsReader | None = None,
        live_price_reader: LivePriceReader | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.heatmap_provider = heatmap_provider
        self.daily_volumes_reader = daily_volumes_reader
        self.daily_candles_reader = daily_candles_reader
        self.eps_reader = eps_reader
        self.live_price_reader = live_price_reader
        self.redis = redis_client

    # ---------- D5 홈 순위표 ----------

    def movers(self, sort: str = "trade", limit: int = DEFAULT_MOVERS_LIMIT) -> dict[str, Any]:
        if sort not in MOVER_SORTS:
            raise ValueError(f"Unsupported sort: {sort}")
        limit = max(1, min(int(limit), MAX_MOVERS_LIMIT))
        payload = self._heatmap_payload()
        items = [item for item in payload.get("items") or [] if isinstance(item, dict) and read_float(item.get("lastPrice")) is not None]
        key_name, descending = {
            "trade": ("sessionDollarVolume", True),
            "volume": ("volume", True),
            "up": ("changePercent", True),
            "down": ("changePercent", False),
        }[sort]
        ranked = [item for item in items if read_float(item.get(key_name)) is not None]
        ranked.sort(key=lambda item: read_float(item.get(key_name)) or 0.0, reverse=descending)
        selected = ranked[:limit]
        averages = self.average_volumes([str(item["symbol"]) for item in selected])
        rows = []
        for item in selected:
            symbol = str(item["symbol"])
            volume = read_float(item.get("volume"))
            ai_summary = self.ai_summary(symbol)
            rows.append({
                "symbol": symbol,
                "nameKo": name_ko(symbol),
                "price": read_float(item.get("lastPrice")),
                "changePercent": read_float(item.get("changePercent")),
                "dollarVolume": read_float(item.get("sessionDollarVolume")),
                "volume": volume,
                "volumeRatio20d": volume_ratio(volume, averages.get(symbol)),
                **ai_summary_fields(ai_summary),
            })
        return {"asOf": payload_as_of(payload), "items": rows}

    # ---------- D9 종목 요약 ----------

    def summary(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        cache_key = SUMMARY_KEY_PREFIX + symbol
        cached = self._json_get(cache_key)
        if isinstance(cached, dict):
            return cached
        payload = self._build_summary(symbol)
        self._json_set(cache_key, payload, SUMMARY_CACHE_TTL_SECONDS)
        return payload

    def _build_summary(self, symbol: str) -> dict[str, Any]:
        heatmap = self._heatmap_payload()
        items = [item for item in heatmap.get("items") or [] if isinstance(item, dict) and isinstance(item.get("symbol"), str)]
        item = next((row for row in items if row["symbol"].upper() == symbol), None)
        candles = self._daily_candles(symbol)
        if item is None and not candles:
            raise SymbolNotFound(symbol)
        item = item or {}
        latest = candles[-1] if candles else {}

        price, previous_close, change, change_percent = price_change(
            self._live_price(symbol), item, candles,
        )

        day_low = read_float(latest.get("low"))
        day_high = read_float(latest.get("high"))
        if price is not None:
            day_low = price if day_low is None else min(day_low, price)
            day_high = price if day_high is None else max(day_high, price)
        year = candles[-WEEKS_52_TRADING_DAYS:]
        lows = [value for value in (read_float(row.get("low")) for row in year) if value is not None]
        highs = [value for value in (read_float(row.get("high")) for row in year) if value is not None]
        if price is not None:
            lows.append(price)
            highs.append(price)

        volume = read_float(item.get("volume"))
        if volume is None:
            volume = read_float(latest.get("volume"))
        dollar_volume = read_float(item.get("sessionDollarVolume"))
        if dollar_volume is None and volume is not None and price is not None:
            dollar_volume = round(volume * price, 2)
        avg_volume = self.average_volumes([symbol]).get(symbol)

        market_cap = read_float(item.get("marketCap"))
        sector = read_text(item.get("sector"))
        by_cap = sorted(
            (row for row in items if read_float(row.get("marketCap")) is not None),
            key=lambda row: read_float(row.get("marketCap")) or 0.0,
            reverse=True,
        )
        market_cap_rank = next((index + 1 for index, row in enumerate(by_cap) if row["symbol"].upper() == symbol), None)
        sector_rows = [row for row in by_cap if sector and read_text(row.get("sector")) == sector][:SECTOR_PER_PEER_LIMIT]
        peer_rows = [row for row in sector_rows if row["symbol"].upper() != symbol][:PEER_COUNT]
        eps = self._eps([symbol, *(row["symbol"].upper() for row in sector_rows)])
        per = price_earnings(price, eps.get(symbol))
        sector_pers = [
            value
            for row in sector_rows
            if (value := price_earnings(
                price if row["symbol"].upper() == symbol else read_float(row.get("lastPrice")),
                eps.get(row["symbol"].upper()),
            )) is not None
        ]

        return {
            "symbol": symbol,
            **korean_name_fields(symbol),
            "price": price,
            "change": change,
            "changePercent": change_percent,
            "marketCap": market_cap,
            "marketCapRank": market_cap_rank,
            "per": per,
            "sectorPer": round(statistics.median(sector_pers), 2) if sector_pers else None,
            "sectorLabelKo": read_text(item.get("sectorLabelKo")),
            "low52": min(lows) if lows else None,
            "high52": max(highs) if highs else None,
            "dayLow": day_low,
            "dayHigh": day_high,
            "volume": volume,
            "avgVolume20d": avg_volume,
            "volumeRatio20d": volume_ratio(volume, avg_volume),
            "dollarVolume": dollar_volume,
            "peers": [
                {
                    "symbol": row["symbol"].upper(),
                    "nameKo": name_ko(row["symbol"]),
                    "per": price_earnings(read_float(row.get("lastPrice")), eps.get(row["symbol"].upper())),
                }
                for row in peer_rows
            ],
            "asOf": payload_as_of(heatmap) or read_text(latest.get("timestamp")),
        }

    # ---------- 오늘 등락 (주문 전 점검) ----------

    def day_change(self, symbol: str) -> dict[str, Any] | None:
        """현재가·전일 종가·오늘 등락률. 종목 요약과 같은 규칙이고, 일봉은 전일 종가가 없을 때만 읽는다."""
        symbol = normalize_symbol(symbol)
        items = self._heatmap_payload().get("items") or []
        item = next((
            row for row in items
            if isinstance(row, dict) and str(row.get("symbol") or "").upper() == symbol
        ), None) or {}
        candles: list[dict[str, Any]] = []
        if read_positive(item.get("previousClose")) is None or read_float(item.get("lastPrice")) is None:
            candles = self._daily_candles(symbol)
        price, previous_close, change, change_percent = price_change(self._live_price(symbol), item, candles)
        if change_percent is None:
            return None
        return {"symbol": symbol, "price": price, "previousClose": previous_close, "change": change, "changePercent": change_percent}

    def _daily_candles(self, symbol: str) -> list[dict[str, Any]]:
        if self.daily_candles_reader is None:
            return []
        try:
            rows = self.daily_candles_reader(symbol, WEEKS_52_TRADING_DAYS) or []
        except Exception:
            logger.exception("stocks daily candle read failed: %s", symbol)
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _live_price(self, symbol: str) -> float | None:
        if self.live_price_reader is None:
            return None
        try:
            return read_positive(self.live_price_reader(symbol))
        except Exception:
            return None

    def _eps(self, symbols: list[str]) -> dict[str, float | None]:
        if self.eps_reader is None:
            return {}
        try:
            return self.eps_reader(list(dict.fromkeys(symbols))) or {}
        except Exception:
            logger.exception("stocks EPS read failed")
            return {}

    # ---------- 공통 ----------

    def _heatmap_payload(self) -> dict[str, Any]:
        try:
            payload = self.heatmap_provider()
        except Exception:
            logger.exception("stocks heatmap projection read failed")
            payload = None
        return payload if isinstance(payload, dict) else {}

    def average_volumes(self, symbols: list[str]) -> dict[str, float | None]:
        """종목별 20일 평균 거래량. Redis 에 1시간 두고, 없는 종목만 한 번에 조회한다."""
        symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
        result: dict[str, float | None] = {}
        missing = []
        for symbol in symbols:
            cached = self._json_get(AVG_VOLUME_KEY_PREFIX + symbol)
            if isinstance(cached, dict) and "avg" in cached:
                result[symbol] = read_float(cached.get("avg"))
            else:
                missing.append(symbol)
        if missing and self.daily_volumes_reader is not None:
            try:
                volumes = self.daily_volumes_reader(missing, VOLUME_AVERAGE_DAYS) or {}
            except Exception:
                logger.exception("stocks daily volume read failed")
                volumes = {}
            for symbol in missing:
                average = average_volume(volumes.get(symbol) or [])
                result[symbol] = average
                if average is not None:
                    self._json_set(AVG_VOLUME_KEY_PREFIX + symbol, {"avg": average}, AVG_VOLUME_CACHE_TTL_SECONDS)
        for symbol in symbols:
            result.setdefault(symbol, None)
        return result

    def ai_summary(self, symbol: str) -> dict[str, Any] | None:
        cached = self._json_get(AI_SUMMARY_KEY_PREFIX + symbol.upper())
        return cached if isinstance(cached, dict) else None

    def _json_get(self, key: str) -> Any:
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _json_set(self, key: str, value: Any, ttl: int) -> None:
        if self.redis is None:
            return
        try:
            self.redis.set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), ex=ttl)
        except Exception:
            return


def get_stocks_service() -> StocksService:
    global _stocks_service
    if _stocks_service is None:
        _stocks_service = build_stocks_service()
    return _stocks_service


def build_stocks_service(provider: Any | None = None) -> StocksService:
    from app.market_data.heatmap.service import MarketHeatmapService
    from app.services.alfaka_market_data import get_market_data_provider

    provider = provider or get_market_data_provider()
    heatmap_service = MarketHeatmapService(provider=provider)
    clickhouse = getattr(provider, "clickhouse_provider", None)
    redis_provider = getattr(provider, "redis_provider", None)

    def heatmap_payload() -> dict[str, Any]:
        # /api/market/heatmap 과 같이 요청 중에는 재계산하지 않는다. 워커가 캐시를 채운다.
        return heatmap_service.snapshot(allow_rebuild=False)

    def daily_volumes(symbols: list[str], days: int) -> dict[str, list[float]]:
        reader = getattr(clickhouse, "recent_daily_volumes", None)
        return reader(symbols, days=days) if callable(reader) else {}

    def daily_candles(symbol: str, limit: int) -> list[dict[str, Any]]:
        reader = getattr(clickhouse, "daily_candles", None)
        return reader(symbol, "1D", limit) if callable(reader) else []

    def live_price(symbol: str) -> float | None:
        reader = getattr(redis_provider, "latest_price", None)
        row = reader(symbol) if callable(reader) else None
        return read_float((row or {}).get("price") or (row or {}).get("lastPrice")) if isinstance(row, dict) else None

    return StocksService(
        heatmap_provider=heatmap_payload,
        daily_volumes_reader=daily_volumes,
        daily_candles_reader=daily_candles,
        eps_reader=lambda symbols: clickhouse_trailing_eps(clickhouse, symbols),
        live_price_reader=live_price,
        redis_client=getattr(redis_provider, "redis", None),
    )


def clickhouse_trailing_eps(clickhouse: Any, symbols: list[str]) -> dict[str, float | None]:
    """SEC 재무 사실(sec_financial_facts)의 eps 로 종목별 최근 12개월 EPS 를 한 번에 구한다.

    EPS 는 sec_derived_metrics 에 없고 sec_financial_facts 에 분기(Q1~Q4)·연간(FY) 값으로 있다.
    """
    query = getattr(clickhouse, "query_json_each_row", None)
    if not callable(query) or not symbols:
        return {}
    table = clickhouse.table("sec_financial_facts") if hasattr(clickhouse, "table") else "market_data.sec_financial_facts"
    rows = query(
        f"""
        SELECT
          symbol,
          fiscal_year AS fiscalYear,
          fiscal_period AS fiscalPeriod,
          toString(period_end) AS periodEndDate,
          value
        FROM {table}
        WHERE symbol IN {{symbols:Array(String)}}
          AND metric = 'eps'
          AND value IS NOT NULL
          AND fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4', 'FY')
          AND period_end >= addMonths(today(), -30)
        ORDER BY symbol ASC, period_end DESC, version_filed_at DESC
        LIMIT 1 BY symbol, fiscal_year, fiscal_period
        FORMAT JSONEachRow
        """,
        {"symbols": symbols},
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        if isinstance(row, dict) and isinstance(row.get("symbol"), str):
            by_symbol.setdefault(row["symbol"].strip().upper(), []).append(row)
    return {symbol: trailing_eps(by_symbol.get(symbol.upper(), [])) for symbol in symbols}


def trailing_eps(rows: list[dict[str, Any]], today: date | None = None) -> float | None:
    """최근 4개 분기 EPS 합. 최근 연간(FY)이 마지막 분기만큼 새롭거나 분기가 모자라면 FY 값을 쓴다."""
    today = today or datetime.now(timezone.utc).date()
    quarters: dict[date, float] = {}
    annual: tuple[date, float] | None = None
    for row in rows:
        end = parse_date(row.get("periodEndDate"))
        value = read_float(row.get("value"))
        if end is None or value is None:
            continue
        if row.get("fiscalPeriod") == "FY":
            if annual is None or end > annual[0]:
                annual = (end, value)
        elif end not in quarters:
            quarters[end] = value
    recent = sorted(quarters.items(), reverse=True)[:4]
    if annual is not None and (not recent or annual[0] >= recent[0][0]):
        return annual[1] if (today - annual[0]).days <= 550 else None
    if len(recent) == 4 and (recent[0][0] - recent[3][0]).days <= 300 and (today - recent[0][0]).days <= 400:
        return round(sum(value for _, value in recent), 4)
    if annual is not None and (today - annual[0]).days <= 550:
        return annual[1]
    return None


def price_earnings(price: float | None, eps: float | None) -> float | None:
    """PER. EPS 가 없거나 0 이하(적자)면 계산하지 않는다."""
    eps = read_float(eps)
    if price is None or eps is None or eps <= 0:
        return None
    return round(price / eps, 2)


def price_change(
    live_price: float | None,
    item: dict[str, Any],
    candles: list[dict[str, Any]],
) -> tuple[float | None, float | None, float | None, float | None]:
    """(현재가, 전일 종가, 등락, 등락률%). 실시간가 → 히트맵 lastPrice → 마지막 일봉 종가 순."""
    latest = candles[-1] if candles else {}
    price = live_price or read_float(item.get("lastPrice")) or read_float(latest.get("close"))
    previous_close = read_positive(item.get("previousClose"))
    if previous_close is None and len(candles) >= 2:
        previous_close = read_positive(candles[-2].get("close"))
    change = round(price - previous_close, 4) if price is not None and previous_close is not None else None
    change_percent = round(change / previous_close * 100, 2) if change is not None else None
    return price, previous_close, change, change_percent


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 15 or not all(char.isalnum() or char in ".-" for char in symbol):
        raise SymbolNotFound(str(value))
    return symbol


def parse_date(value: Any) -> date | None:
    text = read_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def read_positive(value: Any) -> float | None:
    number = read_float(value)
    return number if number is not None and number > 0 else None


def ai_summary_fields(summary: dict[str, Any] | None) -> dict[str, Any]:
    """D14 AI 요약 캐시에서 순위표 한 줄을 꺼낸다. 형태가 달라도 None 으로 둔다."""
    summary = summary or {}
    news = summary.get("news")
    first_news = news[0] if isinstance(news, list) and news and isinstance(news[0], dict) else {}
    return {
        "issue": read_text(summary.get("issueLine")) or issue_from_head(summary.get("head")),
        "issueNewsId": read_text(first_news.get("newsId")),
        "chartNote": read_text(summary.get("chartNote")),
    }


ISSUE_MAX_CHARS = 24


def issue_from_head(head: Any) -> str | None:
    """issueLine 이 없는 예전 캐시용: head 첫 문장을 24자로 자르고 넘치면 '…'."""
    text = read_text(head)
    if text is None:
        return None
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    if len(first) <= ISSUE_MAX_CHARS:
        return first
    return first[: ISSUE_MAX_CHARS - 1].rstrip() + "…"


def average_volume(volumes: list[float]) -> float | None:
    values = [value for value in (read_float(item) for item in volumes[:VOLUME_AVERAGE_DAYS]) if value is not None and value >= 0]
    if len(values) < MIN_VOLUME_AVERAGE_DAYS:
        return None
    return round(sum(values) / len(values), 2)


def volume_ratio(volume: float | None, average: float | None) -> float | None:
    if volume is None or average is None or average <= 0:
        return None
    return round(volume / average, 2)


def payload_as_of(payload: dict[str, Any]) -> str | None:
    return read_text(payload.get("quoteAsOf")) or read_text(payload.get("generatedAt"))


def read_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_text(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value.strip() if isinstance(value, str) and value.strip() else None
