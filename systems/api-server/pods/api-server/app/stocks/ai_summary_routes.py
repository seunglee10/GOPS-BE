"""D14 종목 AI 요약. GET /api/stocks/{symbol}/ai-summary

movers·summary 라우터(app.stocks.routes)와 따로 둔다. 같은 prefix 를 쓰지만 경로가 겹치지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.summary_cache import read_cached_json, summary_cache_redis
from gops_agents.summaries.keys import stock_summary_key

router = APIRouter(prefix="/api/stocks", tags=["stocks"])

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,15}$")
SUMMARY_FIELDS = ("symbol", "generatedAt", "head", "news", "risks", "events", "chartNote")


@router.get("/{symbol}/ai-summary")
def stock_ai_summary(request: Request, symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=400, detail={"code": "invalid_symbol", "symbol": normalized})
    cached = read_cached_json(summary_cache_redis(request), stock_summary_key(normalized))
    if not cached or not cached.get("head"):
        raise HTTPException(status_code=404, detail={"code": "summary_not_ready", "symbol": normalized})
    return {
        "symbol": normalized,
        "generatedAt": cached.get("generatedAt"),
        "head": cached.get("head"),
        "news": _list(cached.get("news")),
        "risks": _list(cached.get("risks")),
        "events": _list(cached.get("events")),
        "chartNote": cached.get("chartNote"),
    }


def _list(value: Any) -> list[Any]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
