from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    configured = path or os.getenv("SIM_ENV_FILE")
    if configured:
        env_path = Path(configured)
    else:
        local_env = PROJECT_ROOT / ".env"
        repository_env = repository_env_path(__file__)
        env_path = local_env if local_env.exists() else repository_env
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value
    return env_path


def repository_env_path(module_file: str | Path) -> Path:
    resolved = Path(module_file).resolve()
    application_env = resolved.parent.parent / ".env"
    if len(resolved.parents) <= 3:
        return application_env
    return resolved.parents[3] / ".env"


def parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return key, clean_env_value(value.strip())


def clean_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value.split(" #", 1)[0].strip()
