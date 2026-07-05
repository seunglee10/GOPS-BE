from __future__ import annotations

from fundamentals.backfill import FundamentalsBackfillConfig, run_companyfacts_backfill


def main() -> None:
    run_companyfacts_backfill(FundamentalsBackfillConfig.from_env())


if __name__ == "__main__":
    main()
