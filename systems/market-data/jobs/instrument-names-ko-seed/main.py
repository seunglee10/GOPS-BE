"""Entrypoint for the instrument Korean-name seed job."""

from __future__ import annotations

from market_data.tools.seed_instrument_names_ko import main as seed_main


if __name__ == "__main__":
    raise SystemExit(seed_main())
