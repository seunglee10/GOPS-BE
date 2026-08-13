"""Entrypoint for the symbol registry sync job."""

from __future__ import annotations

from market_data.tools.sync_symbol_registry import main as sync_main


if __name__ == "__main__":
    sync_main()
