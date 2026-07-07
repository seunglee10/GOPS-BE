from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.auth.dependencies import auth_is_enabled, require_current_user
from app.auth.models import AuthenticatedUser

from .repository import (
    HORIZONS,
    RISK_LEVELS,
    InMemoryRecommendationRepository,
    InvestmentProfileUpsert,
    PostgresRecommendationRepository,
    RecommendationSchemaUnavailable,
)
from .service import RecommendationDataSource, RecommendationService


router = APIRouter(tags=["recommendations"])


class InvestmentProfileBody(BaseModel):
    riskLevel: str = Field(min_length=1, max_length=24)
    horizon: str = "intraday"
    maxDrawdownPct: float = Field(default=6, gt=0, le=50)
    preferredSectors: list[str] = Field(default_factory=list)
    excludedSectors: list[str] = Field(default_factory=list)
    excludedSymbols: list[str] = Field(default_factory=list)


class RefreshBody(BaseModel):
    activeSymbol: str | None = Field(default=None, max_length=12)


@router.get("/api/recommendations/profile")
def get_recommendation_profile(request: Request, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    profile = _call_recommendation_storage(lambda: _repository_from_app(request.app).get_profile(user.sub))
    return {"status": "ready" if profile else "profile_required", "profile": _public_profile(profile) if profile else None}


@router.put("/api/recommendations/profile")
def upsert_recommendation_profile(
    body: InvestmentProfileBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    risk_level = body.riskLevel.strip().lower()
    horizon = body.horizon.strip().lower()
    if risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=422, detail="riskLevel must be conservative, balanced, or aggressive")
    if horizon not in HORIZONS:
        raise HTTPException(status_code=422, detail="v1 recommendations only support intraday horizon")
    profile = _call_recommendation_storage(
        lambda: _repository_from_app(request.app).upsert_profile(
            InvestmentProfileUpsert(
                user_sub=user.sub,
                risk_level=risk_level,
                horizon=horizon,
                max_drawdown_pct=float(body.maxDrawdownPct),
                preferred_sectors=clean_text_list(body.preferredSectors, max_items=12),
                excluded_sectors=clean_text_list(body.excludedSectors, max_items=12),
                excluded_symbols=clean_symbol_list(body.excludedSymbols, max_items=50),
            )
        )
    )
    return {"status": "ready", "profile": _public_profile(profile)}


@router.get("/api/recommendations/stocks/latest")
def latest_stock_recommendations(request: Request, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    return jsonable_encoder(_call_recommendation_storage(lambda: _service_from_app(request.app).latest(user.sub)))


@router.post("/api/recommendations/stocks/refresh")
def refresh_stock_recommendations(
    body: RefreshBody | None,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    now_provider = getattr(request.app.state, "recommendation_now_provider", None)
    now = now_provider() if callable(now_provider) else datetime.now(timezone.utc)
    return jsonable_encoder(
        _call_recommendation_storage(lambda: _service_from_app(request.app).refresh(user.sub, now=now, active_symbol=body.activeSymbol if body else None))
    )


def _call_recommendation_storage(callback):
    try:
        return callback()
    except RecommendationSchemaUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _service_from_app(app: Any) -> RecommendationService:
    existing = getattr(app.state, "recommendation_service", None)
    if existing is not None:
        return existing
    _install_demo_recommendation_providers(app)
    service = RecommendationService(repository=_repository_from_app(app), data_source=RecommendationDataSource(app), app=app)
    app.state.recommendation_service = service
    return service


def _repository_from_app(app: Any):
    existing = getattr(app.state, "recommendation_repository", None)
    if existing is not None:
        return existing
    repository_mode = "memory" if not auth_is_enabled() and not _database_configured() else "postgres"
    if mode := getattr(app.state, "recommendation_repository_mode", None):
        repository_mode = str(mode)
    if repository_mode == "memory":
        repository = InMemoryRecommendationRepository()
    else:
        if not _database_configured():
            raise HTTPException(status_code=503, detail="DATABASE_URL or DATABASE_* settings are required for recommendation API")
        repository = PostgresRecommendationRepository.from_env()
    app.state.recommendation_repository = repository
    return repository


def _install_demo_recommendation_providers(app: Any) -> None:
    if os.getenv("RECOMMENDATION_DEMO_DATA", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if getattr(app.state, "recommendation_demo_data_installed", False):
        return
    app.state.recommendation_demo_data_installed = True
    app.state.recommendation_now_provider = lambda: datetime(2026, 7, 7, 12, 55, tzinfo=ZoneInfo("America/New_York"))
    app.state.recommendation_watchlist_provider = lambda user_sub: ["AAPL"]
    app.state.recommendation_portfolio_provider = lambda user_sub: [
        {"symbol": "NVDA", "sector": "Technology", "marketValueForeign": 8_000},
        {"symbol": "JNJ", "sector": "Healthcare", "marketValueForeign": 12_000},
    ]
    app.state.recommendation_market_provider = lambda: [
        {"symbol": "AAPL", "name": "Apple", "sector": "Technology", "industry": "Consumer Electronics", "sessionDollarVolume": 260_000_000, "changePercent": 3.9, "lastPrice": 214},
        {"symbol": "NVDA", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors", "sessionDollarVolume": 320_000_000, "changePercent": 4.4, "lastPrice": 132},
        {"symbol": "MSFT", "name": "Microsoft", "sector": "Technology", "industry": "Software", "sessionDollarVolume": 210_000_000, "changePercent": 3.4, "lastPrice": 510},
        {"symbol": "AVGO", "name": "Broadcom", "sector": "Technology", "industry": "Semiconductors", "sessionDollarVolume": 180_000_000, "changePercent": 2.9, "lastPrice": 310},
        {"symbol": "LLY", "name": "Eli Lilly", "sector": "Healthcare", "industry": "Drug Manufacturers", "sessionDollarVolume": 130_000_000, "changePercent": 2.6, "lastPrice": 920},
    ]
    app.state.recommendation_candles_provider = _demo_candles
    app.state.recommendation_news_provider = lambda symbol: [{"sentiment": "positive"}] if symbol in {"MSFT", "AVGO", "LLY"} else []


def _demo_candles(symbol: str, _now: datetime) -> list[dict[str, Any]]:
    base_time = datetime(2026, 7, 7, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    rows = []
    for index in range(180):
        if symbol == "SPY":
            close = 500 + index * 0.01
            volume = 300_000
        elif symbol in {"MSFT", "AVGO", "LLY", "AAPL", "NVDA"}:
            close = 100 + index * 0.023
            volume = 100_000 + index * 3_000
        else:
            close = 100 + index * 0.003
            volume = 80_000
        rows.append({
            "timestamp": (base_time + timedelta(minutes=index)).isoformat(),
            "open": close - 0.01,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": volume,
        })
    return rows


def _public_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    return {
        "riskLevel": profile.get("risk_level"),
        "horizon": profile.get("horizon"),
        "maxDrawdownPct": profile.get("max_drawdown_pct"),
        "preferredSectors": profile.get("preferred_sectors") or [],
        "excludedSectors": profile.get("excluded_sectors") or [],
        "excludedSymbols": profile.get("excluded_symbols") or [],
        "updatedAt": profile.get("updated_at"),
    }


def clean_text_list(values: list[str], *, max_items: int) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


def clean_symbol_list(values: list[str], *, max_items: int) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        symbol = value.strip().upper()
        if symbol and symbol not in cleaned:
            cleaned.append(symbol)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _database_configured() -> bool:
    return bool(
        os.getenv("DATABASE_URL")
        or (
            os.getenv("DATABASE_HOST")
            and os.getenv("DATABASE_NAME")
            and os.getenv("DATABASE_USER")
            and os.getenv("DATABASE_PASSWORD")
        )
    )
