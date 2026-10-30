"""사용자 본인 데이터 API(/api/me). D18 최근 본 종목, D34 화면 설정."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.me.repository import (
    RECENT_SYMBOLS_LIMIT,
    InMemoryRecentSymbolsRepository,
    RecentSymbolsRepository,
    RedisRecentSymbolsRepository,
)
from app.me.preferences import (
    EXPERIENCE_VALUES,
    PreferencesRepositoryError,
    UserPreferences,
    VersionConflict,
    preferences_repository_from_app,
)
from app.services.instrument_names import name_ko

router = APIRouter(prefix="/api/me", tags=["me"])

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,16}$")


PREFERENCES_MAX_BYTES = 64 * 1024


class RecentSymbolBody(BaseModel):
    symbol: str


class PreferencesBody(BaseModel):
    version: int = Field(ge=0)
    data: dict[str, Any]


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


def _preferences_payload(prefs: UserPreferences) -> dict[str, Any]:
    return {
        "version": prefs.version,
        "updatedAt": _iso(prefs.updated_at) if prefs.updated_at else None,
        "data": prefs.data,
    }


def _preferences_repository(request: Request):
    try:
        return preferences_repository_from_app(request.app)
    except PreferencesRepositoryError as exc:
        raise HTTPException(status_code=503, detail={"code": "preferences_unavailable"}) from exc


@router.get("/preferences")
def get_preferences(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    try:
        prefs = _preferences_repository(request).get(_user_key(user))
    except PreferencesRepositoryError as exc:
        raise HTTPException(status_code=503, detail={"code": "preferences_unavailable"}) from exc
    return _preferences_payload(prefs)


@router.put("/preferences")
def put_preferences(
    body: PreferencesBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    size = len(json.dumps(body.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > PREFERENCES_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "preferences_too_large", "maxBytes": PREFERENCES_MAX_BYTES, "bytes": size},
        )
    experience = body.data.get("experience")
    if experience is not None and experience not in EXPERIENCE_VALUES:
        raise HTTPException(status_code=422, detail={"code": "invalid_experience"})
    repository = _preferences_repository(request)
    try:
        saved = repository.save(_user_key(user), body.version, body.data, _now(request.app))
    except VersionConflict as conflict:
        raise HTTPException(
            status_code=409,
            detail={"code": "version_conflict", "current": _preferences_payload(conflict.current)},
        ) from None
    except PreferencesRepositoryError as exc:
        raise HTTPException(status_code=503, detail={"code": "preferences_unavailable"}) from exc
    return _preferences_payload(saved)
