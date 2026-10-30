"""홈 순위표(D5)와 종목 요약(D9) 계산.

데이터 출처를 모두 생성자 인자로 받는다. 테스트는 가짜 함수를 넣고,
운영은 build_stocks_service() 가 히트맵 projection·ClickHouse·Redis 를 연결한다.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from typing import Any

from app.services.instrument_names import name_ko

logger = logging.getLogger(__name__)

MOVER_SORTS = ("trade", "volume", "up", "down")
DEFAULT_MOVERS_LIMIT = 20
MAX_MOVERS_LIMIT = 100
VOLUME_AVERAGE_DAYS = 20
MIN_VOLUME_AVERAGE_DAYS = 5
AVG_VOLUME_CACHE_TTL_SECONDS = 3600
AI_SUMMARY_KEY_PREFIX = "gops:stocks:ai-summary:"
AVG_VOLUME_KEY_PREFIX = "gops:stocks:avg-volume-20d:"

HeatmapProvider = Callable[[], dict[str, Any]]
DailyVolumesReader = Callable[[list[str], int], dict[str, list[float]]]

_stocks_service: "StocksService | None" = None


class StocksService:
    def __init__(
        self,
        *,
        heatmap_provider: HeatmapProvider,
        daily_volumes_reader: DailyVolumesReader | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.heatmap_provider = heatmap_provider
        self.daily_volumes_reader = daily_volumes_reader
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

    return StocksService(
        heatmap_provider=heatmap_payload,
        daily_volumes_reader=daily_volumes,
        redis_client=getattr(redis_provider, "redis", None),
    )


def ai_summary_fields(summary: dict[str, Any] | None) -> dict[str, Any]:
    """D14 AI 요약 캐시에서 순위표 한 줄을 꺼낸다. 형태가 달라도 None 으로 둔다."""
    summary = summary or {}
    news = summary.get("news")
    first_news = news[0] if isinstance(news, list) and news and isinstance(news[0], dict) else {}
    return {
        "issue": read_text(summary.get("head")),
        "issueNewsId": read_text(first_news.get("newsId")),
        "chartNote": read_text(summary.get("chartNote")),
    }


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
