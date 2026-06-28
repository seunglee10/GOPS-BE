"""Entrypoint for the KIS broker adapter pod."""

from __future__ import annotations

import os
import shlex

from kis_trader.cli import main as kis_trader_main


def main() -> int:
    extra_args = os.getenv("KIS_BROKER_ADAPTER_ARGS")
    if not extra_args:
        extra_args = "--fake-kis success"
    args = ["broker-adapter", "--timeout-seconds", "1.0", *shlex.split(extra_args)]
    return kis_trader_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
