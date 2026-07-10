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
    replay_speed: float = 60.0
    replay_date: str = "latest"
    replay_lookback_days: int = 1
    replay_max_delay_seconds: float = 0.25
    replay_batch_size: int = 1
    replay_loop: bool = True
    replay_loop_pause_seconds: float = 1.0
    replay_rewrite_timestamps: bool = True
    replay_wall_clock_timestamps: bool = True
    randomize_ticks: bool = True
    random_seed: int | None = None
    tick_price_jitter_bps: float = 8.0
    tick_size_jitter_ratio: float = 0.5
    quote_spread_bps: float = 4.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        data_root = Path(os.getenv("SIM_DATA_ROOT", str(PROJECT_ROOT / "data" / "sessions")))
        return cls(
            data_root=data_root,
            auth_mode=os.getenv("SIM_AUTH_MODE", "dev").strip().lower(),
            api_key_id=os.getenv("SIM_API_KEY_ID"),
            api_secret_key=os.getenv("SIM_API_SECRET_KEY"),
            replay_speed=_float_env("SIM_REPLAY_SPEED", 60.0),
            replay_date=os.getenv("SIM_REPLAY_DATE", "latest").strip() or "latest",
            replay_lookback_days=max(1, _int_env("SIM_REPLAY_LOOKBACK_DAYS", 1)),
            replay_max_delay_seconds=_float_env("SIM_REPLAY_MAX_DELAY_SECONDS", 0.25),
            replay_batch_size=max(1, _int_env("SIM_REPLAY_BATCH_SIZE", 1)),
            replay_loop=_bool_env("SIM_REPLAY_LOOP", True),
            replay_loop_pause_seconds=max(0.0, _float_env("SIM_REPLAY_LOOP_PAUSE_SECONDS", 1.0)),
            replay_rewrite_timestamps=_bool_env("SIM_REPLAY_REWRITE_TIMESTAMPS", True),
            replay_wall_clock_timestamps=_bool_env("SIM_REPLAY_WALL_CLOCK_TIMESTAMPS", True),
            randomize_ticks=_bool_env("SIM_RANDOMIZE_TICKS", True),
            random_seed=_optional_int_env("SIM_RANDOM_SEED"),
            tick_price_jitter_bps=max(0.0, _float_env("SIM_TICK_PRICE_JITTER_BPS", 8.0)),
            tick_size_jitter_ratio=max(0.0, _float_env("SIM_TICK_SIZE_JITTER_RATIO", 0.5)),
            quote_spread_bps=max(0.0, _float_env("SIM_QUOTE_SPREAD_BPS", 4.0)),
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


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
