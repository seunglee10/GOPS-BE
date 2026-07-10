from __future__ import annotations

from fastapi import Request

from gops_simul.config import Settings
from gops_simul.errors import Unauthorized


KEY_HEADER = "APCA-API-KEY-ID"
SECRET_HEADER = "APCA-API-SECRET-KEY"


def validate_rest_auth(request: Request, settings: Settings) -> None:
    validate_credentials(
        key=request.headers.get(KEY_HEADER),
        secret=request.headers.get(SECRET_HEADER),
        settings=settings,
    )


def validate_credentials(key: str | None, secret: str | None, settings: Settings) -> None:
    mode = settings.auth_mode
    if mode in {"off", "none", "disabled"}:
        return
    if not key or not secret:
        raise Unauthorized("authentication headers are missing or invalid")
    if mode == "strict":
        if key != settings.api_key_id or secret != settings.api_secret_key:
            raise Unauthorized("authentication headers are missing or invalid")
    elif mode != "dev":
        raise Unauthorized(f"unsupported simulator auth mode: {mode}")
