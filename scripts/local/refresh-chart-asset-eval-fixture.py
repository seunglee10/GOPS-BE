#!/usr/bin/env python3
"""Download real Nasdaq daily history for the chart-asset evaluation corpus."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from-date", required=True, help="ISO date")
    parser.add_argument("--to-date", required=True, help="ISO date")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    query = urllib.parse.urlencode({
        "assetclass": "stocks",
        "fromdate": args.from_date,
        "todate": args.to_date,
        "limit": 5000,
    })
    request = urllib.request.Request(
        f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/historical?{query}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    source_rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or [])
    rows = [_normalize_row(symbol, row) for row in reversed(source_rows)]
    rows = [row for row in rows if row is not None]
    if not rows:
        raise RuntimeError("Nasdaq returned no valid daily rows")
    output = Path(args.output)
    output.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"symbol": symbol, "bars": len(rows), "from": rows[0]["timestamp"], "to": rows[-1]["timestamp"]}))
    return 0


def _normalize_row(symbol: str, row: dict) -> dict | None:
    try:
        day = datetime.strptime(str(row["date"]), "%m/%d/%Y").date()
        values = {key: _number(row[key]) for key in ("open", "high", "low", "close")}
        volume = int(_number(row.get("volume") or 0))
    except (KeyError, TypeError, ValueError):
        return None
    if min(values.values()) <= 0 or values["high"] < values["low"]:
        return None
    day_key = day.strftime("%Y%m%d")
    return {
        "symbol": symbol,
        "timestamp": f"{day.isoformat()}T00:00:00.000Z",
        **values,
        "volume": volume,
        "isClosed": True,
        "canonicalVersion": "v2",
        "priceAdjustment": "split",
        "marketSession": "regular",
        "sourceClass": "clickhouse_direct",
        "sourceEventId": f"nasdaq-{symbol}-{day_key}",
    }


def _number(value) -> float:
    return float(str(value).replace("$", "").replace(",", "").strip())


if __name__ == "__main__":
    raise SystemExit(main())
