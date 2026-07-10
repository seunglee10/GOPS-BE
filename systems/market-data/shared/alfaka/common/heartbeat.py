from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


DEFAULT_HEARTBEAT_PATH = "/tmp/gops-worker-heartbeat"


def heartbeat_path() -> Path:
    return Path(os.getenv("GOPS_WORKER_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH))


def touch_heartbeat(*, now: float | None = None) -> Path:
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    if now is not None:
        os.utime(path, (now, now))
    return path


def heartbeat_is_fresh(max_age_seconds: float, *, now: float | None = None) -> bool:
    path = heartbeat_path()
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return current - modified <= max(0.0, max_age_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a GOPS worker loop heartbeat file.")
    parser.add_argument("--max-age-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    return 0 if heartbeat_is_fresh(args.max_age_seconds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
