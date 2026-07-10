from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


DEFAULT_HEARTBEAT_PATH = "/tmp/gops-worker-heartbeat"


def heartbeat_path() -> Path:
    return Path(os.getenv("GOPS_WORKER_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH))


def touch_heartbeat() -> None:
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def heartbeat_is_fresh(max_age_seconds: float) -> bool:
    try:
        modified = heartbeat_path().stat().st_mtime
    except OSError:
        return False
    return time.time() - modified <= max(0.0, max_age_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a GOPS order worker heartbeat file.")
    parser.add_argument("--max-age-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    return 0 if heartbeat_is_fresh(args.max_age_seconds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
