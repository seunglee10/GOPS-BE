"""Entrypoint for the limited order reconciliation job."""

from __future__ import annotations

from kis_trader.cli import main as kis_trader_main


def main() -> int:
    return kis_trader_main(["reconcile", "--rows-json", "[]"])


if __name__ == "__main__":
    raise SystemExit(main())
