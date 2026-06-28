import os
from pathlib import Path

CORS_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5173",
    "http://localhost:5174",
]


def read_dotenv_value(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]

    current_file = Path(__file__).resolve()
    candidates = [Path.cwd() / ".env"]
    candidates.extend(parent / ".env" for parent in current_file.parents)
    seen: set[Path] = set()
    for env_path in candidates:
        env_path = env_path.resolve()
        if env_path in seen or not env_path.exists():
            continue
        seen.add(env_path)
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("\"'")
    return None
