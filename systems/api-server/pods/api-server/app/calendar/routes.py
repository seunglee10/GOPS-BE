"""D28 경제·실적 캘린더. 누구나 볼 수 있고, 로그인하면 관심·보유 종목 실적에 level="watch" 를 붙인다.

GET /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&kind=econ|earn&region=kr|us
GET /api/calendar/brief?week=YYYY-Www

- 경제지표: Postgres economic_events(economic-calendar 잡이 Finnhub 에서 6시간마다 적재)
- 실적: ClickHouse yahoo_earnings_estimates 의 S&P 500 실적일(차트 실적 표시와 같은 원천)
- 주간 요약: stock-summary-refresh 잡이 Redis gops:calendar-brief:{week} 에 써 둔 값
날짜·시각은 한국 시간이다. 기간은 최대 62일.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.dependencies import optional_current_user
from app.auth.models import AuthenticatedUser
from app.calendar.repository import EconomicEventRepository, InMemoryEconomicEventRepository, PostgresEconomicEventRepository
from app.services.summary_cache import read_cached_json, summary_cache_redis
from gops_agents.summaries.keys import calendar_brief_key
from market_data.economic_calendar.items import build_earnings_items, econ_item, sort_items

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

SEOUL = ZoneInfo("Asia/Seoul")
MAX_RANGE_DAYS = 62
TOP_MARKET_CAP = 50
WEEK_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")


# ---------- 의존성(app.state 로 바꿔 끼운다) ----------

def economic_event_repository_from_app(app: Any) -> EconomicEventRepository | None:
    existing = getattr(app.state, "economic_event_repository", None)
    if existing is not None:
        return existing
    if getattr(app.state, "economic_event_repository_mode", "") == "memory":
        repository: EconomicEventRepository = InMemoryEconomicEventRepository()
    else:
        from app.auth.identity import database_conninfo

        conninfo = database_conninfo()
        if not conninfo:
            return None
        repository = PostgresEconomicEventRepository(conninfo)
    app.state.economic_event_repository = repository
    return repository


def _earnings_rows(app: Any, from_time: str, to_time: str) -> list[dict[str, Any]]:
    provider: Callable[[str, str], list[dict[str, Any]]] | None = getattr(app.state, "calendar_earnings_provider", None)
    if provider is None:
        from app.services.alfaka_market_data import get_market_data_provider

        clickhouse = getattr(get_market_data_provider(), "clickhouse_provider", None)
        provider = getattr(clickhouse, "earnings_events_between", None)
        if not callable(provider):
            return []
    try:
        return [row for row in provider(from_time, to_time) or [] if isinstance(row, dict)]
    except Exception as exc:  # 실적 원천이 죽어도 경제지표는 보여 준다.
        logger.warning("calendar earnings read failed: %s", exc)
        return []


def _universe(app: Any) -> set[str] | None:
    provider = getattr(app.state, "calendar_universe_provider", None)
    if callable(provider):
        return set(provider())
    from app.services.alfaka_market_data import sp500_universe_symbols

    symbols = sp500_universe_symbols(fallback_to_configured=False)
    return set(symbols) if symbols else None


def _major_symbols(app: Any) -> set[str]:
    provider = getattr(app.state, "calendar_major_symbols_provider", None)
    if callable(provider):
        return set(provider())
    from gops_agents.summaries.sources import seed_market_cap_symbols, top_market_cap

    items: list[dict[str, Any]] = []
    try:
        from app.market_data.heatmap.service import get_heatmap_service

        payload = get_heatmap_service().snapshot("sp500", allow_rebuild=False)
        items = [item for item in (payload or {}).get("items") or [] if isinstance(item, dict)]
    except Exception:
        items = []
    return set(top_market_cap(items, TOP_MARKET_CAP) or seed_market_cap_symbols(TOP_MARKET_CAP))


def _personal_symbols(app: Any, user: AuthenticatedUser | None) -> set[str]:
    if user is None:
        return set()
    provider = getattr(app.state, "calendar_user_symbols_provider", None)
    if callable(provider):
        return {str(symbol).upper() for symbol in provider(user)}
    symbols: set[str] = set()
    try:
        from app.services.alfaka_market_data import read_watchlist_symbols

        symbols.update(read_watchlist_symbols(user.app_user_id or user.sub, legacy_user_id=user.sub if user.app_user_id else None))
    except Exception as exc:
        logger.warning("calendar watchlist read failed: %s", exc)
    try:
        from app.routes.paper_trading import paper_repository_from_app

        snapshot = paper_repository_from_app(app).account_snapshot(user.sub)
        symbols.update(str(row.get("symbol") or "") for row in snapshot.get("positions") or [] if isinstance(row, dict))
    except Exception as exc:
        logger.warning("calendar holdings read failed: %s", exc)
    return {symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()}


def _name_ko(symbol: str) -> str | None:
    from app.services.instrument_names import name_ko

    return name_ko(symbol)


def _today(app: Any) -> date:
    clock: Callable[[], datetime] | None = getattr(app.state, "calendar_clock", None)
    now = clock() if callable(clock) else datetime.now(timezone.utc)
    return now.astimezone(SEOUL).date()


# ---------- 검증 ----------

def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={"code": "invalid_date", "field": field}) from None


def parse_week(value: str) -> tuple[date, date]:
    match = WEEK_PATTERN.fullmatch(value or "")
    if not match:
        raise HTTPException(status_code=422, detail={"code": "invalid_week"})
    try:
        monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_week"}) from None
    return monday, monday + timedelta(days=6)


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


# ---------- 라우트 ----------

@router.get("")
def calendar_items(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    kind: str | None = Query(default=None, pattern="^(econ|earn)$"),
    region: str | None = Query(default=None, pattern="^(kr|us)$"),
    user: AuthenticatedUser | None = Depends(optional_current_user),
) -> dict[str, Any]:
    from_date = _parse_date(from_, "from")
    to_date = _parse_date(to, "to")
    if to_date < from_date:
        raise HTTPException(status_code=422, detail={"code": "invalid_range"})
    if (to_date - from_date).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail={"code": "range_too_large", "maxDays": MAX_RANGE_DAYS})

    app = request.app
    items: list[dict[str, Any]] = []
    if kind in (None, "econ"):
        repository = economic_event_repository_from_app(app)
        if repository is not None:
            try:
                rows = repository.between(from_date, to_date, region)
            except Exception as exc:
                logger.warning("calendar economic events read failed: %s", exc)
                raise HTTPException(status_code=503, detail={"code": "calendar_unavailable"}) from exc
            items.extend(econ_item(row) for row in rows)
    if kind in (None, "earn") and region in (None, "us"):
        # 한국 날짜 from~to 를 UTC 구간(from 전날 15시 ~ to 당일 15시)으로 바꿔 읽는다.
        start = datetime.combine(from_date, time.min, SEOUL).astimezone(timezone.utc)
        end = datetime.combine(to_date + timedelta(days=1), time.min, SEOUL).astimezone(timezone.utc)
        rows = _earnings_rows(app, _iso(start), _iso(end))
        if rows:
            items.extend(
                build_earnings_items(
                    rows,
                    name_ko=_name_ko,
                    personal=_personal_symbols(app, user),
                    major=_major_symbols(app),
                    universe=_universe(app),
                    from_date=from_date,
                    to_date=to_date,
                )
            )
    return {"items": sort_items(items)}


@router.get("/brief")
def calendar_brief(request: Request, week: str | None = Query(default=None)) -> dict[str, Any]:
    week = week or iso_week(_today(request.app))
    parse_week(week)
    cached = read_cached_json(summary_cache_redis(request), calendar_brief_key(week))
    text = cached.get("text") if cached else None
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=404, detail={"code": "brief_not_ready"})
    return {"week": week, "text": text, "generatedAt": cached.get("generatedAt")}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
