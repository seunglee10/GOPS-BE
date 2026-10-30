"""사용자 본인 데이터 API(/api/me). D18 최근 본 종목. 이후 D34 preferences 도 여기에 둡니다."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.me.repository import (
    RECENT_SYMBOLS_LIMIT,
    InMemoryRecentSymbolsRepository,
    RecentSymbolsRepository,
    RedisRecentSymbolsRepository,
)
from app.services.instrument_names import name_ko

router = APIRouter(prefix="/api/me", tags=["me"])

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,16}$")


class RecentSymbolBody(BaseModel):
    symbol: str


def recent_symbols_repository_from_app(app: Any) -> RecentSymbolsRepository:
    existing = getattr(app.state, "recent_symbols_repository", None)
    if existing is not None:
        return existing
    mode = str(getattr(app.state, "recent_symbols_repository_mode", "") or "redis")
    repository: RecentSymbolsRepository
    if mode == "memory":
        repository = InMemoryRecentSymbolsRepository()
    else:
        try:
            repository = RedisRecentSymbolsRepository.from_env()
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "recent_symbols_unavailable"}) from exc
    app.state.recent_symbols_repository = repository
    return repository


def _user_key(user: AuthenticatedUser) -> str:
    return str(user.app_user_id or user.sub)


def _now(app: Any) -> datetime:
    provider: Callable[[], datetime] | None = getattr(app.state, "recent_symbols_now_provider", None)
    return provider() if callable(provider) else datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.get("/recent-symbols")
def list_recent_symbols(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = recent_symbols_repository_from_app(request.app)
    try:
        rows = repository.list(_user_key(user), RECENT_SYMBOLS_LIMIT)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "recent_symbols_unavailable"}) from exc
    return {
        "items": [
            {"symbol": symbol, "nameKo": name_ko(symbol), "viewedAt": _iso(viewed_at)}
            for symbol, viewed_at in rows
        ]
    }


@router.post("/recent-symbols", status_code=204)
def add_recent_symbol(
    body: RecentSymbolBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> Response:
    symbol = body.symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise HTTPException(status_code=422, detail={"code": "invalid_symbol"})
    repository = recent_symbols_repository_from_app(request.app)
    try:
        repository.add(_user_key(user), symbol, _now(request.app))
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "recent_symbols_unavailable"}) from exc
    return Response(status_code=204)
