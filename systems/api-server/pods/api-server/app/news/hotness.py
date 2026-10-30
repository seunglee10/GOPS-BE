"""isHot: 관련 종목 중 하나라도 오늘 거래량이 20일 평균의 1.5배 이상이면 True.

오늘 거래량은 히트맵 projection 캐시의 volume 을 그대로 쓴다(요청 중 재계산 없음).
20일 평균은 D9 종목 요약의 volumeRatio20d 와 같은 값이 되도록 StocksService.average_volumes
(ClickHouse 마감 일봉 묶음 조회 + 종목별 Redis 1시간 캐시)를 그대로 쓴다.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable

DEFAULT_HOT_VOLUME_RATIO = 1.5


def hot_volume_ratio_from_env() -> float:
    try:
        value = float(os.getenv("NEWS_HOT_VOLUME_RATIO", ""))
    except ValueError:
        return DEFAULT_HOT_VOLUME_RATIO
    return value if value > 0 else DEFAULT_HOT_VOLUME_RATIO


class NewsHotness:
    def __init__(
        self,
        *,
        today_volumes: Callable[[], dict[str, float]],
        average_volumes: Callable[[list[str]], dict[str, float | None]],
        ratio: float | None = None,
    ) -> None:
        self._today_volumes = today_volumes
        self._average_volumes = average_volumes
        self._ratio = ratio if ratio is not None else hot_volume_ratio_from_env()

    def hot_symbols(self, symbols: Iterable[str]) -> set[str]:
        wanted = list(dict.fromkeys(str(s or "").strip().upper() for s in symbols if str(s or "").strip()))
        if not wanted:
            return set()
        try:
            today = {str(k).upper(): float(v) for k, v in (self._today_volumes() or {}).items() if v}
        except Exception:
            return set()
        # 오늘 거래량이 없는 종목은 평균을 구할 필요가 없다.
        candidates = [symbol for symbol in wanted if today.get(symbol, 0) > 0]
        if not candidates:
            return set()
        try:
            averages = self._average_volumes(candidates) or {}
        except Exception:
            return set()
        hot = set()
        for symbol in candidates:
            average = averages.get(symbol)
            if average and average > 0 and today[symbol] / average >= self._ratio:
                hot.add(symbol)
        return hot


def heatmap_today_volumes(payload: dict[str, Any]) -> dict[str, float]:
    volumes: dict[str, float] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        try:
            volume = float(item.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and volume > 0:
            volumes[symbol] = volume
    return volumes


def news_hotness_for_provider(provider: Any) -> NewsHotness:
    from app.market_data.heatmap.service import get_heatmap_service
    from app.stocks.service import build_stocks_service

    stocks = build_stocks_service(provider)
    return NewsHotness(
        today_volumes=lambda: heatmap_today_volumes(get_heatmap_service(provider).snapshot(allow_rebuild=False)),
        average_volumes=stocks.average_volumes,
    )
