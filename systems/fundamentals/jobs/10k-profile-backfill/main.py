from __future__ import annotations

from fundamentals.ten_k_profiles import TenKProfileBackfillConfig, run_ten_k_profile_backfill


def main() -> None:
    run_ten_k_profile_backfill(TenKProfileBackfillConfig.from_env())


if __name__ == "__main__":
    main()
