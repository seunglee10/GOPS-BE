import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import dotenv_values, load_dotenv


load_dotenv(override=True)
DOTENV_VALUES = dotenv_values(".env")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    openai_timeout_seconds: float


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    timeout_raw = _env_value("OPENAI_TIMEOUT_SECONDS", "20")
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 20.0

    return Settings(
        openai_api_key=_env_value("OPENAI_API_KEY", ""),
        openai_model=_env_value("OPENAI_MODEL", "gpt-5.5") or "gpt-5.5",
        openai_timeout_seconds=timeout,
    )


def _env_value(name: str, default: str) -> str:
    if name in DOTENV_VALUES:
        value = DOTENV_VALUES.get(name)
        return "" if value is None else value
    return os.getenv(name, default)
