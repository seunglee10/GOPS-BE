"""Entrypoint for the order database migration job."""

from __future__ import annotations

from kis_trader.cli import main as kis_trader_main


def main() -> int:
    return kis_trader_main(["migrate"])


if __name__ == "__main__":
    raise SystemExit(main())
