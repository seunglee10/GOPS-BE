"""Entrypoint for the market-data backfill worker pod."""

from __future__ import annotations

from alfaka.backfill.worker import main as worker_main


if __name__ == "__main__":
    worker_main()
