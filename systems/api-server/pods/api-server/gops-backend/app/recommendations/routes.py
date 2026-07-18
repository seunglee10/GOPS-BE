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
from app.core.sectors import normalize_sector_list

from .repository import (
    HORIZONS,
    RECOMMENDATION_STYLES,
    RISK_LEVELS,
    InMemoryRecommendationRepository,
    InvestmentProfileUpsert,
    PostgresRecommendationRepository,
    RecommendationSchemaUnavailable,
    ScoreProfileUpsert,
)
from .score_profiles import (
    MAX_CUSTOM_SCORE_PROFILES,
    SCORE_PROFILE_SCHEMA_VERSION,
    ScoreProfileValidationError,
    normalize_score_profile_payload,
    public_score_profile,
    system_score_profile,
)
from .fixed_replay import FixedReplayProviderError, decision_v1_enabled, fixed_replay_provider
from .profile_suggestions import suggest_score_profile
from .service import RecommendationDataSource, RecommendationService, active_score_profile


router = APIRouter(tags=["recommendations"])


class InvestmentProfileBody(BaseModel):
    riskLevel: str = Field(min_length=1, max_length=24)
    recommendationStyle: str = Field(default="balanced", min_length=1, max_length=24)
    horizon: str = "intraday"
    maxDrawdownPct: float = Field(default=6, gt=0, le=50)
    preferredSectors: list[str] = Field(default_factory=list)
    excludedSectors: list[str] = Field(default_factory=list)
    excludedSymbols: list[str] = Field(default_factory=list)


class RefreshBody(BaseModel):
    activeSymbol: str | None = Field(default=None, max_length=12)


class ScoreProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    blockWeights: dict[str, float]
    factorWeights: dict[str, dict[str, float]]
    portfolioWeight: float
    portfolioFactorWeights: dict[str, float]


class ActiveScoreProfileBody(BaseModel):
    type: str = Field(pattern="^(preset|custom)$")
    presetStyle: str | None = None
    profileId: int | None = Field(default=None, gt=0)


class ScoreProfileSuggestionBody(BaseModel):
    query: str = Field(min_length=2, max_length=500)


@router.get("/api/recommendations/profile")
def get_recommendation_profile(request: Request, user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    profile = _call_recommendation_storage(lambda: repository.get_profile(user.sub))
    return {
        "status": "ready" if profile else "profile_required",
        "profile": _public_profile(_profile_with_active_score(repository, profile)) if profile else None,
    }


@router.put("/api/recommendations/profile")
def upsert_recommendation_profile(
    body: InvestmentProfileBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    risk_level = body.riskLevel.strip().lower()
    recommendation_style = body.recommendationStyle.strip().lower()
    horizon = body.horizon.strip().lower()
    if risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=422, detail="riskLevel must be conservative, balanced, or aggressive")
    if recommendation_style not in RECOMMENDATION_STYLES:
        raise HTTPException(status_code=422, detail="recommendationStyle must be momentum, balanced, or stable")
    if horizon not in HORIZONS:
        raise HTTPException(status_code=422, detail="v1 recommendations only support intraday horizon")
    profile = _call_recommendation_storage(
        lambda: _repository_from_app(request.app).upsert_profile(
            InvestmentProfileUpsert(
                user_sub=user.sub,
                risk_level=risk_level,
                recommendation_style=recommendation_style,
                horizon=horizon,
                max_drawdown_pct=float(body.maxDrawdownPct),
                preferred_sectors=clean_sector_list(body.preferredSectors, max_items=12),
                excluded_sectors=clean_sector_list(body.excludedSectors, max_items=12),
                excluded_symbols=clean_symbol_list(body.excludedSymbols, max_items=50),
            )
        )
    )
    return {
        "status": "ready",
        "profile": _public_profile(_profile_with_active_score(_repository_from_app(request.app), profile)),
    }


@router.get("/api/recommendations/score-profiles")
def list_recommendation_score_profiles(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    profile = _call_recommendation_storage(lambda: repository.get_profile(user.sub))
    risk_level = str((profile or {}).get("risk_level") or "balanced")
    custom = _call_recommendation_storage(lambda: repository.list_score_profiles(user.sub))
    active_id = (profile or {}).get("active_score_profile_id")
    active = next((public_score_profile(row) for row in custom if row.get("id") == active_id), None)
    if active is None:
        active = system_score_profile(str((profile or {}).get("recommendation_style") or "balanced"), risk_level)
    return {
        "schemaVersion": SCORE_PROFILE_SCHEMA_VERSION,
        "maxCustomProfiles": MAX_CUSTOM_SCORE_PROFILES,
        "presets": [system_score_profile(style, risk_level) for style in ("momentum", "balanced", "stable")],
        "customProfiles": [public_score_profile(row) for row in custom],
        "active": active,
    }


@router.post("/api/recommendations/score-profiles")
def create_recommendation_score_profile(
    body: ScoreProfileBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    normalized = _validated_score_profile_body(body)
    try:
        row = _call_recommendation_storage(lambda: repository.create_score_profile(
            ScoreProfileUpsert(user_sub=user.sub, name=body.name.strip(), **normalized),
            max_profiles=MAX_CUSTOM_SCORE_PROFILES,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "created", "profile": public_score_profile(row)}


@router.post("/api/recommendations/score-profiles/suggestions")
def suggest_recommendation_score_profile(
    body: ScoreProfileSuggestionBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    try:
        suggestion = _call_recommendation_storage(
            lambda: suggest_score_profile(request.app, repository, user.sub, body.query.strip())
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="추천 로직 AI 제안을 생성하지 못했습니다.") from exc
    return {"status": "ready", "suggestion": suggestion}


@router.put("/api/recommendations/score-profiles/{profile_id:int}")
def update_recommendation_score_profile(
    profile_id: int,
    body: ScoreProfileBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    normalized = _validated_score_profile_body(body)
    try:
        row = _call_recommendation_storage(lambda: repository.update_score_profile(
            profile_id, ScoreProfileUpsert(user_sub=user.sub, name=body.name.strip(), **normalized)
        ))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="score profile not found")
    return {"status": "updated", "profile": public_score_profile(row)}


@router.delete("/api/recommendations/score-profiles/{profile_id:int}")
def delete_recommendation_score_profile(
    profile_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    deleted = _call_recommendation_storage(lambda: repository.delete_score_profile(user.sub, profile_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="score profile not found")
    return {"status": "deleted", "activeFallback": "balanced"}


@router.put("/api/recommendations/score-profiles/active")
def activate_recommendation_score_profile(
    body: ActiveScoreProfileBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    if body.type == "preset":
        style = str(body.presetStyle or "").strip().lower()
        if style not in RECOMMENDATION_STYLES:
            raise HTTPException(status_code=422, detail="presetStyle must be momentum, balanced, or stable")
        profile_id = None
    else:
        if body.profileId is None:
            raise HTTPException(status_code=422, detail="profileId is required for a custom score profile")
        profile_id = body.profileId
        style = "balanced"
    repository = _repository_from_app(request.app)
    profile = _call_recommendation_storage(
        lambda: repository.activate_score_profile(user.sub, profile_id, preset_style=style)
    )
    if not profile:
        raise HTTPException(status_code=404, detail="investment or score profile not found")
    enriched = _profile_with_active_score(repository, profile)
    return {"status": "active", "profile": _public_profile(enriched)}


@router.get("/api/recommendations/stocks/latest")
def latest_stock_recommendations(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    provider = _fixed_replay_from_app(request.app)
    if provider is not None:
        return jsonable_encoder(_fixed_replay_response(request.app, provider, user.sub))
    return jsonable_encoder(_call_recommendation_storage(lambda: _service_from_app(request.app).latest(user.sub)))


@router.post("/api/recommendations/stocks/refresh")
def refresh_stock_recommendations(
    body: RefreshBody | None,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    provider = _fixed_replay_from_app(request.app)
    if provider is not None:
        return jsonable_encoder(_fixed_replay_response(request.app, provider, user.sub))
    now_provider = getattr(request.app.state, "recommendation_now_provider", None)
    now = now_provider() if callable(now_provider) else datetime.now(timezone.utc)
    return jsonable_encoder(
        _call_recommendation_storage(
            lambda: _service_from_app(request.app).refresh(
                user.sub,
                now=now,
                active_symbol=body.activeSymbol if body else None,
            )
        )
    )


def _call_recommendation_storage(callback):
    try:
        return callback()
    except RecommendationSchemaUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _fixed_replay_from_app(app: Any):
    try:
        return fixed_replay_provider(app)
    except FixedReplayProviderError as exc:
        raise HTTPException(status_code=503, detail="fixed_replay_recommendation_unavailable") from exc


def _fixed_replay_response(app: Any, provider: Any, user_sub: str) -> dict[str, Any]:
    if not decision_v1_enabled():
        return provider.response()
    repository = _repository_from_app(app)
    cutoff = datetime.fromisoformat(str(provider.payload["evidenceAsOf"]))
    get_profile = getattr(repository, "get_profile", None)
    current_profile = get_profile(user_sub) if callable(get_profile) else None
    historical_profile = repository.get_profile_at(user_sub, cutoff)
    profile = historical_profile or current_profile
    score_profile = (
        active_score_profile(repository, current_profile)
        if current_profile and callable(getattr(repository, "list_score_profiles", None))
        else system_score_profile(
            str((profile or {}).get("recommendation_style") or "balanced"),
            str((profile or {}).get("risk_level") or "balanced"),
        )
    )
    portfolio_snapshot, portfolio_evaluated_at = _fixed_replay_portfolio_context(
        app,
        repository,
        user_sub,
        cutoff,
    )
    return _call_recommendation_storage(
        lambda: provider.response(
            profile=profile,
            portfolio_snapshot=portfolio_snapshot,
            score_profile=score_profile,
            portfolio_evaluated_at=portfolio_evaluated_at,
        )
    )


def _fixed_replay_portfolio_context(
    app: Any,
    repository: Any,
    user_sub: str,
    cutoff: datetime,
) -> tuple[dict[str, Any] | None, datetime]:
    cutoff_snapshot = repository.get_portfolio_snapshot_at(user_sub, cutoff)
    try:
        from app.routes.simulator import simulator_gateway_from_app

        status = simulator_gateway_from_app(app).status()
    except Exception:
        return cutoff_snapshot, cutoff
    run_id = str(status.get("runId") or "").strip()
    virtual_time = _fixed_replay_datetime(status.get("virtualTime"))
    if status.get("mode") != "simulation" or not run_id or virtual_time is None:
        return cutoff_snapshot, cutoff
    get_current_snapshot = getattr(repository, "get_portfolio_snapshot", None)
    if not callable(get_current_snapshot):
        return cutoff_snapshot, cutoff
    current_snapshot = get_current_snapshot(user_sub)
    payload = (
        current_snapshot.get("payload")
        if isinstance(current_snapshot, dict) and isinstance(current_snapshot.get("payload"), dict)
        else current_snapshot
    )
    if not isinstance(payload, dict):
        return cutoff_snapshot, cutoff
    observed_at = _fixed_replay_datetime(
        (current_snapshot.get("source_as_of") if isinstance(current_snapshot, dict) else None)
        or payload.get("sourceAsOf")
        or payload.get("asOf")
    )
    if (
        payload.get("simulation") is not True
        or str(payload.get("runId") or "").strip() != run_id
        or observed_at is None
        or observed_at > virtual_time
    ):
        return cutoff_snapshot, cutoff
    return current_snapshot, virtual_time


def _fixed_replay_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    app.state.recommendation_news_provider = lambda symbol, now=None: [
        {"sentiment": "positive", "publishedAt": (now or datetime.now(timezone.utc)).isoformat()}
    ] if symbol in {"MSFT", "AVGO", "LLY"} else []


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
        "recommendationStyle": profile.get("recommendation_style") or "balanced",
        "horizon": profile.get("horizon"),
        "maxDrawdownPct": profile.get("max_drawdown_pct"),
        "preferredSectors": normalize_sector_list(profile.get("preferred_sectors") or []),
        "excludedSectors": normalize_sector_list(profile.get("excluded_sectors") or []),
        "excludedSymbols": profile.get("excluded_symbols") or [],
        "updatedAt": profile.get("updated_at"),
        "profileRevision": profile.get("profile_revision") or 1,
        "activeScoreProfile": profile.get("active_score_profile"),
    }


def _profile_with_active_score(repository: Any, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    payload = dict(profile)
    active_id = profile.get("active_score_profile_id")
    rows = repository.list_score_profiles(str(profile.get("user_sub") or "")) if active_id else []
    active = next((public_score_profile(row) for row in rows if row.get("id") == active_id), None)
    payload["active_score_profile"] = active or system_score_profile(
        str(profile.get("recommendation_style") or "balanced"),
        str(profile.get("risk_level") or "balanced"),
    )
    return payload


def _validated_score_profile_body(body: ScoreProfileBody) -> dict[str, Any]:
    try:
        normalized = normalize_score_profile_payload(body.model_dump())
    except ScoreProfileValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "schema_version": normalized["schemaVersion"],
        "block_weights": normalized["blockWeights"],
        "factor_weights": normalized["factorWeights"],
        "portfolio_weight": normalized["portfolioWeight"],
        "portfolio_factor_weights": normalized["portfolioFactorWeights"],
    }


def clean_sector_list(values: list[str], *, max_items: int) -> list[str]:
    return normalize_sector_list(values, max_items=max_items)


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
