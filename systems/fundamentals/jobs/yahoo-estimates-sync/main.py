from __future__ import annotations

from fundamentals.yahoo_estimates import YahooEstimatesConfig, run_yahoo_estimates_sync


def main() -> None:
    run_yahoo_estimates_sync(YahooEstimatesConfig.from_env())


if __name__ == "__main__":
    main()
