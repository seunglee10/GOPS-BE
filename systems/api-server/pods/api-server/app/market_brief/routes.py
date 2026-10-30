"""D15 오늘 시장 요약. GET /api/market-brief/today

미국 동부 기준 오늘 요약이 있으면 그것을, 없으면 3일 안의 가장 최근 요약을 돌려준다(marketDate 로 날짜 표시).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from app.services.summary_cache import read_cached_json, summary_cache_redis
from gops_agents.summaries.keys import market_brief_key

router = APIRouter(prefix="/api/market-brief", tags=["market-brief"])

NEW_YORK = ZoneInfo("America/New_York")
LOOKBACK_DAYS = 3


def _today(request: Request) -> date:
    clock: Callable[[], datetime] | None = getattr(request.app.state, "market_brief_clock", None)
    now = clock() if clock is not None else datetime.now(timezone.utc)
    return now.astimezone(NEW_YORK).date()


@router.get("/today")
def market_brief_today(request: Request) -> dict[str, Any]:
    redis_client = summary_cache_redis(request)
    today = _today(request)
    for offset in range(LOOKBACK_DAYS + 1):
        market_date = (today - timedelta(days=offset)).isoformat()
        cached = read_cached_json(redis_client, market_brief_key(market_date))
        paragraphs = cached.get("paragraphs") if cached else None
        if isinstance(paragraphs, list) and paragraphs:
            return {
                "marketDate": cached.get("marketDate") or market_date,
                "generatedAt": cached.get("generatedAt"),
                "paragraphs": [item for item in paragraphs if isinstance(item, dict)],
            }
    raise HTTPException(status_code=404, detail={"code": "brief_not_ready"})
