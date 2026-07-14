from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable

from gops_simul.config import PROJECT_ROOT
from gops_simul.env import load_env_file
from gops_simul.time_utils import parse_record_time, parse_time
from gops_simul.tools.import_alpaca import DATA_BASE_URL, alpaca_headers, fetch_kind


PRE_SOURCE_START = "2026-07-07T19:58:00Z"
PRE_SOURCE_END = "2026-07-07T20:00:00Z"
POST_SOURCE_START = "2026-07-08T13:18:00Z"
POST_SOURCE_END = "2026-07-08T13:30:00Z"
LEGACY_SCENARIO_ID = "iran-ceasefire-collapse-2026-07-08"
LEGACY_DEMO_SYMBOLS = ("NVDA", "AMD", "AVGO", "MU", "TSM", "XOM", "CVX", "COP")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build the GOPS five-minute Iran-news scenario from real Alpaca trades."
    )
    parser.add_argument("--env-file", help="Environment file containing Alpaca API credentials.")
    parser.add_argument("--base-url", default=DATA_BASE_URL)
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--bucket-seconds", type=float, default=0.25)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "data" / "scenarios" / LEGACY_SCENARIO_ID),
    )
    args = parser.parse_args(argv)
    load_env_file(args.env_file, override=True)
    headers = alpaca_headers()

    pre_rows: list[dict[str, object]] = []
    post_rows: list[dict[str, object]] = []
    for symbol in LEGACY_DEMO_SYMBOLS:
        pre_rows.extend(fetch_kind(
            kind="trades",
            base_url=args.base_url,
            symbol=symbol,
            feed=args.feed,
            start=PRE_SOURCE_START,
            end=PRE_SOURCE_END,
            limit=10_000,
            max_pages=args.max_pages,
            headers=headers,
        ))
        post_rows.extend(fetch_kind(
            kind="trades",
            base_url=args.base_url,
            symbol=symbol,
            feed=args.feed,
            start=POST_SOURCE_START,
            end=POST_SOURCE_END,
            limit=10_000,
            max_pages=args.max_pages,
            headers=headers,
        ))
        print(f"[FETCH] {symbol}: pre={count_symbol(pre_rows, symbol)} post={count_symbol(post_rows, symbol)}")

    seed_prices = last_prices(pre_rows)
    missing = [symbol for symbol in LEGACY_DEMO_SYMBOLS if symbol not in seed_prices]
    if missing:
        raise SystemExit(f"No pre-event trades returned for: {', '.join(missing)}")

    events = [
        *compress_events(
            pre_rows,
            source_start=PRE_SOURCE_START,
            source_end=PRE_SOURCE_END,
            target_start=0.0,
            target_end=5.0,
            bucket_seconds=args.bucket_seconds,
        ),
        *compress_events(
            post_rows,
            source_start=POST_SOURCE_START,
            source_end=POST_SOURCE_END,
            target_start=5.0,
            target_end=300.0,
            bucket_seconds=args.bucket_seconds,
        ),
    ]
    events.sort(key=lambda row: (float(row["atSeconds"]), str(row["payload"].get("S") or "")))

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "scenarioId": LEGACY_SCENARIO_ID,
        "title": "Iran ceasefire collapse · semiconductor to energy rotation",
        "durationSeconds": 300,
        "breakingNewsAtSeconds": 5,
        "seedPrices": seed_prices,
        "symbols": list(LEGACY_DEMO_SYMBOLS),
        "source": {
            "provider": "Alpaca Market Data API",
            "feed": args.feed,
            "preEventWindow": {"start": PRE_SOURCE_START, "end": PRE_SOURCE_END},
            "postEventWindow": {"start": POST_SOURCE_START, "end": POST_SOURCE_END},
            "timeCompression": "2 minutes to 5 seconds; 12 minutes to 295 seconds",
            "generatedEventCount": len(events),
        },
        "breakingNews": {
            "id": "iran-ceasefire-over-2026-07-08",
            "headline": "[속보] 이란 휴전 붕괴 우려…유가 급등·글로벌 증시 흔들",
            "summary": "미국 대통령이 이란과의 휴전이 끝났다고 언급하면서 유가가 뛰고 위험자산 변동성이 확대됐습니다.",
            "source": "AP",
            "url": "https://apnews.com/article/671d9c94b302f7db533f46baa18387d3",
            "symbols": list(LEGACY_DEMO_SYMBOLS),
        },
    }
    (output / "scenario.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "events.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in events) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] wrote {len(events)} replay events to {output}")


def map_scenario_second(
    timestamp: str,
    *,
    source_start: str,
    source_end: str,
    target_start: float,
    target_end: float,
) -> float:
    value = parse_time(timestamp)
    start = parse_time(source_start)
    end = parse_time(source_end)
    if value is None or start is None or end is None or end <= start:
        raise ValueError("valid source timestamp range is required")
    ratio = (value - start).total_seconds() / (end - start).total_seconds()
    return target_start + min(1.0, max(0.0, ratio)) * (target_end - target_start)


def compress_events(
    rows: Iterable[dict[str, object]],
    *,
    source_start: str,
    source_end: str,
    target_start: float,
    target_end: float,
    bucket_seconds: float,
) -> list[dict[str, object]]:
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be positive")
    selected: dict[tuple[str, int], tuple[float, dict[str, object]]] = {}
    for row in sorted(rows, key=parse_record_time):
        symbol = str(row.get("S") or "").upper()
        timestamp = str(row.get("t") or "")
        if not symbol or not timestamp:
            continue
        at_seconds = map_scenario_second(
            timestamp,
            source_start=source_start,
            source_end=source_end,
            target_start=target_start,
            target_end=target_end,
        )
        bucket = math.floor((at_seconds - target_start) / bucket_seconds)
        selected[(symbol, bucket)] = (at_seconds, dict(row))
    result = []
    for at_seconds, payload in sorted(selected.values(), key=lambda item: (item[0], str(item[1].get("S") or ""))):
        result.append({
            "atSeconds": round(at_seconds, 6),
            "sourceTimestamp": payload.get("t"),
            "payload": payload,
        })
    return result


def last_prices(rows: Iterable[dict[str, object]]) -> dict[str, float]:
    values: dict[str, tuple[object, float]] = {}
    for row in rows:
        try:
            symbol = str(row["S"]).upper()
            price = float(row["p"])
            timestamp = parse_record_time(row)
        except (KeyError, TypeError, ValueError):
            continue
        current = values.get(symbol)
        if current is None or timestamp > current[0]:
            values[symbol] = (timestamp, price)
    return {symbol: value[1] for symbol, value in values.items()}


def count_symbol(rows: Iterable[dict[str, object]], symbol: str) -> int:
    return sum(1 for row in rows if row.get("S") == symbol)


if __name__ == "__main__":
    main()
