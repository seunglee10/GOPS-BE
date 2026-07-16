from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from gops_simul.env import load_env_file


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_root: Path = PROJECT_ROOT / "data" / "sessions"
    auth_mode: str = "dev"
    api_key_id: str | None = None
    api_secret_key: str | None = None
    replay_speed: float = 1.0
    replay_date: str = "2026-07-15"
    replay_lookback_days: int = 1

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        data_root = Path(os.getenv("SIM_DATA_ROOT", str(PROJECT_ROOT / "data" / "sessions")))
        return cls(
            data_root=data_root,
            auth_mode=os.getenv("SIM_AUTH_MODE", "dev").strip().lower(),
            api_key_id=os.getenv("SIM_API_KEY_ID"),
            api_secret_key=os.getenv("SIM_API_SECRET_KEY"),
            replay_speed=_float_env("SIM_REPLAY_SPEED", 1.0),
            replay_date=os.getenv("SIM_REPLAY_DATE", "2026-07-15").strip() or "2026-07-15",
            replay_lookback_days=max(1, _int_env("SIM_REPLAY_LOOKBACK_DAYS", 1)),
        )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
