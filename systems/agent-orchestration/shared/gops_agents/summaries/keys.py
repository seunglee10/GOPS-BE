"""Redis 키와 TTL 계약. API 서버(D14/D15/D28 라우트, D5 movers)가 같은 값을 읽는다.

키에는 REDIS_KEY_PREFIX 를 붙이지 않는다. 문서 docs/STOCK_SUMMARIES.md 와 같이 고정 문자열이다.
"""

from __future__ import annotations

STOCK_SUMMARY_KEY_PREFIX = "gops:stocks:ai-summary:"
MARKET_BRIEF_KEY_PREFIX = "gops:market-brief:"
CALENDAR_BRIEF_KEY_PREFIX = "gops:calendar-brief:"

STOCK_SUMMARY_TTL_SECONDS = 24 * 60 * 60
MARKET_BRIEF_TTL_SECONDS = 3 * 24 * 60 * 60
CALENDAR_BRIEF_TTL_SECONDS = 8 * 24 * 60 * 60


def stock_summary_key(symbol: str) -> str:
    return f"{STOCK_SUMMARY_KEY_PREFIX}{str(symbol or '').strip().upper()}"


def market_brief_key(market_date: str) -> str:
    """market_date 는 미국 동부 기준 YYYY-MM-DD."""
    return f"{MARKET_BRIEF_KEY_PREFIX}{str(market_date or '').strip()}"


def calendar_brief_key(week: str) -> str:
    """week 는 한국 시간 기준 ISO 주 YYYY-Www(예: 2026-W44)."""
    return f"{CALENDAR_BRIEF_KEY_PREFIX}{str(week or '').strip()}"
