from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from gops_simul.config import PROJECT_ROOT
from gops_simul.env import load_env_file
from gops_simul.errors import BadRequest
from gops_simul.storage import normalize_symbols


DATA_BASE_URL = "https://data.alpaca.markets"
DEFAULT_IMPORT_DAYS = 7


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Import Alpaca market-data sessions into simulator JSONL fixtures.")
    parser.add_argument("--symbol", action="append", default=[], help="Symbol to import. May be repeated.")
    parser.add_argument("--symbols", help="Comma-separated symbols to import, for example AAPL,TSLA.")
    parser.add_argument("--date", help="Single trading date in YYYY-MM-DD.")
    parser.add_argument("--start-date", help="Inclusive first date in YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Inclusive last date in YYYY-MM-DD. Defaults to today in UTC.")
    parser.add_argument("--days", type=int, default=DEFAULT_IMPORT_DAYS, help="Calendar-day lookback when --date is not set.")
    parser.add_argument("--include-weekends", action="store_true", help="Also request Saturday/Sunday sessions.")
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--data-root", default=str(PROJECT_ROOT / "data" / "sessions"))
    parser.add_argument("--base-url", default=DATA_BASE_URL)
    parser.add_argument("--kind", choices=["all", "bars", "trades", "quotes"], default="all")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        symbols = selected_symbols(args.symbol, args.symbols)
        session_dates = selected_dates(
            date_arg=args.date,
            start_date_arg=args.start_date,
            end_date_arg=args.end_date,
            days=args.days,
            include_weekends=args.include_weekends,
            today=datetime.now(UTC).date(),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (args.start or args.end) and len(session_dates) != 1:
        raise SystemExit("--start/--end can only be used with a single --date import.")

    headers = alpaca_headers()
    selected = ["bars", "trades", "quotes"] if args.kind == "all" else [args.kind]
    for symbol in symbols:
        for session_date in session_dates:
            start = args.start or utc_regular_open(session_date)
            end = args.end or utc_regular_close(session_date)
            output_dir = Path(args.data_root) / args.feed / symbol / session_date.isoformat()
            output_dir.mkdir(parents=True, exist_ok=True)

            for kind in selected:
                rows = fetch_kind(
                    kind=kind,
                    base_url=args.base_url,
                    symbol=symbol,
                    feed=args.feed,
                    start=start,
                    end=end,
                    limit=args.limit,
                    max_pages=args.max_pages,
                    headers=headers,
                )
                path = output_dir / file_name_for_kind(kind)
                write_jsonl(path, rows)
                print(f"[OK] wrote {len(rows)} {symbol} {session_date.isoformat()} {kind} rows to {path}")


def selected_symbols(symbol_args: list[str], symbols_csv: str | None) -> list[str]:
    raw_values = list(symbol_args)
    if symbols_csv:
        raw_values.extend(part.strip() for part in symbols_csv.split(","))
    if not any(value.strip() for value in raw_values):
        raise ValueError("--symbol or --symbols is required.")
    try:
        return normalize_symbols(raw_values)
    except BadRequest as exc:
        raise ValueError(str(exc)) from exc


def selected_dates(
    *,
    date_arg: str | None,
    start_date_arg: str | None,
    end_date_arg: str | None,
    days: int,
    include_weekends: bool,
    today: date,
) -> list[date]:
    if date_arg:
        if start_date_arg or end_date_arg:
            raise ValueError("--date cannot be combined with --start-date/--end-date.")
        return [date.fromisoformat(date_arg)]
    if days < 1:
        raise ValueError("--days must be at least 1.")

    end_date = date.fromisoformat(end_date_arg) if end_date_arg else today
    start_date = date.fromisoformat(start_date_arg) if start_date_arg else end_date - timedelta(days=days - 1)
    if start_date > end_date:
        raise ValueError("--start-date must be before or equal to --end-date.")

    values = [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]
    if include_weekends:
        return values
    weekdays = [value for value in values if value.weekday() < 5]
    if not weekdays:
        raise ValueError("No weekdays selected. Increase --days or pass --include-weekends.")
    return weekdays


def fetch_kind(
    *,
    kind: str,
    base_url: str,
    symbol: str,
    feed: str,
    start: str,
    end: str,
    limit: int,
    max_pages: int,
    headers: dict[str, str],
) -> list[dict[str, object]]:
    page_token = None
    rows: list[dict[str, object]] = []
    for _ in range(max_pages):
        url = build_url(
            kind=kind,
            base_url=base_url,
            symbol=symbol,
            feed=feed,
            start=start,
            end=end,
            limit=limit,
            page_token=page_token,
        )
        payload = fetch_json(url, headers)
        rows.extend(normalize_rows(kind, symbol, payload))
        page_token = payload.get("next_page_token") if isinstance(payload, dict) else None
        if not page_token:
            break
    return rows


def build_url(
    *,
    kind: str,
    base_url: str,
    symbol: str,
    feed: str,
    start: str,
    end: str,
    limit: int,
    page_token: str | None,
) -> str:
    params = {
        "symbols": symbol,
        "start": start,
        "end": end,
        "limit": str(limit),
        "feed": feed,
        "sort": "asc",
    }
    if kind == "bars":
        params["timeframe"] = "1Min"
        params["adjustment"] = "split"
    if page_token:
        params["page_token"] = page_token
    return f"{base_url.rstrip('/')}/v2/stocks/{kind}?{urllib.parse.urlencode(params)}"


def fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_rows(kind: str, symbol: str, payload: dict[str, Any]) -> list[dict[str, object]]:
    rows = (payload.get(kind) or {}).get(symbol, [])
    result = []
    message_type = {"bars": "b", "trades": "t", "quotes": "q"}[kind]
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = {"T": message_type, "S": symbol, **row}
        result.append(normalized)
    return result


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    body = "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def alpaca_headers() -> dict[str, str]:
    key = first_env("APCA_API_KEY_ID", "ALPACA_API_KEY_ID")
    secret = first_env("APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("APCA_API_KEY_ID/APCA_API_SECRET_KEY or ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY are required.")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def utc_regular_open(value: date) -> str:
    return datetime.combine(value, time(13, 30), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_regular_close(value: date) -> str:
    return datetime.combine(value, time(20, 0), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def file_name_for_kind(kind: str) -> str:
    return {"bars": "bars_1m.jsonl", "trades": "trades.jsonl", "quotes": "quotes.jsonl"}[kind]


if __name__ == "__main__":
    main()
