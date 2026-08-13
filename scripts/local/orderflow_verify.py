#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parents[2]
shared_path = repo_root / "systems" / "market-data" / "shared"
if str(shared_path) not in sys.path:
    sys.path.insert(0, str(shared_path))

from market_data.orderflow.config import price_bin_size_from_env  # noqa: E402
from market_data.orderflow.rollup import create_clickhouse_client_from_env  # noqa: E402
from market_data.orderflow.verify import build_order_flow_verification_report, print_human_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare live, as-of tick, and daily-row order-flow profiles.")
    parser.add_argument("--symbol", required=True, help="Ticker symbol, for example NVDA.")
    parser.add_argument("--date", required=True, help="Regular session date in YYYY-MM-DD.")
    parser.add_argument("--minutes-detail", action="store_true", help="Include per-minute metrics in JSON output.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--skip-live", action="store_true", help="Do not call the live intraday API.")
    parser.add_argument("--api-base-url", default=None, help="API base URL for live intraday reads.")
    parser.add_argument("--price-bin-size", type=float, default=price_bin_size_from_env())
    args = parser.parse_args(argv)

    client = create_clickhouse_client_from_env()
    report = build_order_flow_verification_report(
        client,
        args.symbol,
        args.date,
        price_bin_size=args.price_bin_size,
        fetch_live=not args.skip_live,
        api_base_url=args.api_base_url,
        include_minutes=args.minutes_detail,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print_human_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
