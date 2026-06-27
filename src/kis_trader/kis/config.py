"""KIS Open API demo configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class KisConfigError(RuntimeError):
    """Raised when KIS configuration is missing or unsafe."""


@dataclass(frozen=True)
class KisConfig:
    env: str
    app_key: str
    app_secret: str
    account_no: str
    product_code: str
    base_url: str
    token_cache_path: Path
    user_agent: str
    contact_phone: str
    mgco_aptm_odno: str
    order_server_code: str
    timeout_seconds: float


def load_kis_config(env: str | None = None, env_file: str | Path | None = None) -> KisConfig:
    if env_file is not None:
        load_dotenv(env_file)
    else:
        load_dotenv()

    selected_env = (env or os.getenv("KIS_ENV", "demo")).strip().lower()
    if selected_env != "demo":
        raise KisConfigError("Only KIS demo trading is implemented. KIS_ENV=real is not allowed.")

    required = {
        "KIS_DEMO_APP_KEY": os.getenv("KIS_DEMO_APP_KEY", "").strip(),
        "KIS_DEMO_APP_SECRET": os.getenv("KIS_DEMO_APP_SECRET", "").strip(),
        "KIS_DEMO_ACCOUNT_NO": os.getenv("KIS_DEMO_ACCOUNT_NO", "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise KisConfigError("Missing required environment variables: " + ", ".join(missing))

    try:
        timeout_seconds = float(os.getenv("KIS_TIMEOUT_SECONDS", "10").strip())
    except ValueError as exc:
        raise KisConfigError("KIS_TIMEOUT_SECONDS must be numeric.") from exc

    return KisConfig(
        env="demo",
        app_key=required["KIS_DEMO_APP_KEY"],
        app_secret=required["KIS_DEMO_APP_SECRET"],
        account_no=required["KIS_DEMO_ACCOUNT_NO"],
        product_code=os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01").strip() or "01",
        base_url=os.getenv("KIS_DEMO_BASE_URL", "https://openapivts.koreainvestment.com:29443").strip(),
        token_cache_path=Path(os.getenv("KIS_TOKEN_CACHE_PATH", ".kis_token_cache.json")).expanduser(),
        user_agent=os.getenv(
            "KIS_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        ).strip(),
        contact_phone=os.getenv("KIS_CONTACT_PHONE", "").strip(),
        mgco_aptm_odno=os.getenv("KIS_MGCO_APTM_ODNO", "").strip(),
        order_server_code=os.getenv("KIS_ORDER_SERVER_CODE", "0").strip() or "0",
        timeout_seconds=timeout_seconds,
    )
