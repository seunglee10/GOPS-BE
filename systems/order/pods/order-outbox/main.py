"""Entrypoint for the long-running order outbox publisher pod."""

from __future__ import annotations

import time
import traceback

from kis_trader.cli import main as kis_trader_main
from kis_trader.runtime_heartbeat import touch_heartbeat


def main() -> int:
    touch_heartbeat()
    while True:
        try:
            exit_code = kis_trader_main(["outbox-publish", "--limit", "100"])
            if exit_code:
                print(f"order-outbox publish exited with code {exit_code}", flush=True)
        except Exception:
            traceback.print_exc()
        touch_heartbeat()
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
