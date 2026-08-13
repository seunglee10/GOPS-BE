"""Layout preset routes: per-user list of saved workspace layouts.

Write access requires an authenticated user; the list is stored server-side in PostgreSQL
(``user_layout_presets``) so it follows the account across devices. Anonymous users keep
presets locally on the client.
"""

import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

try:
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover - matches the fallback used in app/routes/charts.py
    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def Field(default=None, **kwargs):  # type: ignore[no-redef]
        if "default_factory" in kwargs and default is None:
            return kwargs["default_factory"]()
        return default

from app.auth.dependencies import auth_is_enabled, require_current_user
from app.auth.models import AuthenticatedUser
from app.services.chart_presets_repository import (
    InMemoryChartPresetsRepository,
    PostgresChartPresetsRepository,
)

router = APIRouter()

MAX_PRESETS = 50


class LayoutPresetBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    layout: dict[str, Any] = Field(default_factory=dict)
    role: Literal["incident-response"] | None = None


class LayoutPresetsRequestBody(BaseModel):
    presets: list[LayoutPresetBody] = Field(default_factory=list)


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


def _repository_from_app(app: Any):
    existing = getattr(app.state, "chart_presets_repository", None)
    if existing is not None:
        return existing
    repository_mode = "memory" if not auth_is_enabled() and not _database_configured() else "postgres"
    if mode := getattr(app.state, "chart_presets_repository_mode", None):
        repository_mode = str(mode)
    if repository_mode == "memory":
        repository = InMemoryChartPresetsRepository()
    else:
        if not _database_configured():
            raise HTTPException(status_code=503, detail="DATABASE_URL or DATABASE_* settings are required for layout preset API")
        repository = PostgresChartPresetsRepository.from_env()
    app.state.chart_presets_repository = repository
    return repository


def _serialize(preset: LayoutPresetBody) -> dict[str, Any]:
    serialized = {"id": preset.id, "name": preset.name, "layout": preset.layout}
    if preset.role is not None:
        serialized["role"] = preset.role
    return serialized


@router.get("/api/charts/presets")
def chart_presets_list(
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    return {"presets": repository.read_presets(user.sub)}


@router.put("/api/charts/presets")
def chart_presets_replace(
    request: Request,
    body: LayoutPresetsRequestBody,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    repository = _repository_from_app(request.app)
    payload = [_serialize(preset) for preset in body.presets][:MAX_PRESETS]
    return {"presets": repository.replace_presets(user.sub, payload)}
