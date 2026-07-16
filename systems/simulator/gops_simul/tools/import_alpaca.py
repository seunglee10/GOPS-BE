from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator

from gops_simul.config import PROJECT_ROOT
from gops_simul.dataset import DATASET_ID, FEED_SEGMENTS, REPLAY_SYMBOLS, dataset_manifest_template, isoformat_z
from gops_simul.env import load_env_file
from gops_simul.errors import BadRequest
from gops_simul.storage import normalize_symbols


DATA_BASE_URL = "https://data.alpaca.markets"
DEFAULT_IMPORT_DAYS = 7
CLICKHOUSE_INSERT_BATCH_SIZE = 50_000


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Import Alpaca market data or build the fixed replay dataset.")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--symbols")
    parser.add_argument("--date")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--days", type=int, default=DEFAULT_IMPORT_DAYS)
    parser.add_argument("--include-weekends", action="store_true")
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--data-root", default=str(PROJECT_ROOT / "data" / "sessions"))
    parser.add_argument("--base-url", default=DATA_BASE_URL)
    parser.add_argument("--kind", choices=["all", "bars", "trades", "quotes"], default="all")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--fixed-dataset", action="store_true")
    parser.add_argument("--s3-uri", default=default_replay_s3_uri())
    parser.add_argument("--clickhouse-url", default=os.getenv("CLICKHOUSE_URL"))
    parser.add_argument("--clickhouse-database", default=os.getenv("CLICKHOUSE_DATABASE", "market_data"))
    parser.add_argument("--clickhouse-user", default=os.getenv("CLICKHOUSE_USER", "default"))
    parser.add_argument("--clickhouse-password", default=os.getenv("CLICKHOUSE_PASSWORD", ""))
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args(argv)

    if args.fixed_dataset:
        if not args.local_only and (not args.s3_uri or not args.clickhouse_url):
            raise SystemExit("--fixed-dataset requires --s3-uri and --clickhouse-url unless --local-only is set")
        manifest = build_fixed_dataset(output_root=Path(args.data_root), base_url=args.base_url,
            headers=alpaca_headers(), limit=args.limit, max_pages=args.max_pages, s3_uri=args.s3_uri,
            clickhouse_url=args.clickhouse_url, clickhouse_database=args.clickhouse_database,
            clickhouse_user=args.clickhouse_user, clickhouse_password=args.clickhouse_password,
            local_only=args.local_only)
        print(json.dumps(manifest, ensure_ascii=False, indent=2)); return

    symbols = selected_symbols(args.symbol, args.symbols)
    dates = selected_dates(date_arg=args.date, start_date_arg=args.start_date, end_date_arg=args.end_date,
        days=args.days, include_weekends=args.include_weekends, today=datetime.now(UTC).date())
    if (args.start or args.end) and len(dates) != 1: raise SystemExit("--start/--end require one date")
    kinds = ["bars", "trades", "quotes"] if args.kind == "all" else [args.kind]
    for symbol in symbols:
        for day in dates:
            start, end = args.start or utc_regular_open(day), args.end or utc_regular_close(day)
            output = Path(args.data_root) / args.feed / symbol / day.isoformat(); output.mkdir(parents=True, exist_ok=True)
            for kind in kinds:
                rows = fetch_kind(kind=kind, base_url=args.base_url, symbol=symbol, feed=args.feed,
                    start=start, end=end, limit=args.limit, max_pages=args.max_pages, headers=alpaca_headers())
                write_jsonl(output / file_name_for_kind(kind), rows)


def fetch_kind(*, kind: str, base_url: str, symbol: str, feed: str, start: str, end: str,
               limit: int, headers: dict[str, str], max_pages: int | None = None) -> list[dict[str, object]]:
    return list(iter_kind_rows(kind=kind, base_url=base_url, symbol=symbol, feed=feed, start=start,
        end=end, limit=limit, headers=headers, max_pages=max_pages))


def iter_kind_rows(*, kind: str, base_url: str, symbol: str, feed: str, start: str, end: str,
                   limit: int, headers: dict[str, str], max_pages: int | None = None) -> Iterator[dict[str, object]]:
    page_token = None; seen: set[str] = set(); pages = 0
    while True:
        if max_pages is not None and pages >= max_pages: raise RuntimeError(f"pagination truncated after {max_pages} pages")
        payload = fetch_json(build_url(kind=kind, base_url=base_url, symbol=symbol, feed=feed,
            start=start, end=end, limit=limit, page_token=page_token), headers); pages += 1
        for row in normalize_rows(kind, symbol, payload):
            if _timestamp_in_half_open_window(row.get("t"), start, end): yield row
        page_token = payload.get("next_page_token") if isinstance(payload, dict) else None
        if not page_token: break
        token = str(page_token)
        if token in seen: raise RuntimeError("Alpaca returned a repeated next_page_token")
        seen.add(token)


def build_fixed_dataset(*, output_root: Path, base_url: str, headers: dict[str, str], limit: int,
                        max_pages: int | None, s3_uri: str | None, clickhouse_url: str | None,
                        clickhouse_database: str, clickhouse_user: str, clickhouse_password: str,
                        local_only: bool) -> dict[str, object]:
    root = output_root / DATASET_ID; manifest_path = root / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()).get("status") in {"READY", "LOCAL_READY"}:
        raise RuntimeError(f"immutable dataset already exists at {root}")
    root.mkdir(parents=True, exist_ok=True)
    s3_prefix = str(s3_uri or "").rstrip("/")
    if s3_prefix and _s3_manifest_exists(s3_prefix): raise RuntimeError(f"immutable S3 dataset already exists at {s3_prefix}")
    clickhouse = None
    if clickhouse_url:
        from gops_simul.clickhouse import ClickHouseHttpClient
        clickhouse = ClickHouseHttpClient(clickhouse_url, database=clickhouse_database, user=clickhouse_user,
            password=clickhouse_password, timeout_seconds=120)
        _clear_clickhouse_build(clickhouse)
    manifest = dataset_manifest_template(); manifest["createdAt"] = datetime.now(UTC).isoformat()
    manifest["source"] = {"provider": "alpaca", "baseUrl": base_url, "adjustment": "raw", "limit": limit}
    counts = manifest["counts"]; collection_sequence = 0
    try:
        for segment_index, segment in enumerate(FEED_SEGMENTS, 1):
            for symbol in REPLAY_SYMBOLS:
                symbol_counts = counts["bySymbol"].setdefault(symbol, {"trades": 0, "quotes": 0})
                for kind in ("trades", "quotes"):
                    relative = Path(f"feed={segment.feed}/segment={segment_index:02d}/symbol={symbol}/{kind}.jsonl.gz")
                    path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
                    row_count = 0; batch: list[dict[str, object]] = []
                    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
                        for row_count, row in enumerate(iter_kind_rows(kind=kind, base_url=base_url, symbol=symbol,
                            feed=segment.feed, start=isoformat_z(segment.start), end=isoformat_z(segment.end),
                            limit=limit, max_pages=max_pages, headers=headers), 1):
                            collection_sequence += 1
                            encoded = json.dumps(row, sort_keys=True, separators=(",", ":")); handle.write(encoded + "\n")
                            if clickhouse:
                                batch.append({"dataset_id": DATASET_ID, "event_time": row["t"], "source_file": str(relative),
                                    "source_sequence": collection_sequence, "symbol": symbol,
                                    "event_type": "trade" if kind == "trades" else "quote", "feed": segment.feed, "payload": encoded})
                                if len(batch) >= CLICKHOUSE_INSERT_BATCH_SIZE:
                                    clickhouse.insert_json_each_row("market_data.simulation_replay_staging", batch); batch.clear()
                    if clickhouse and batch: clickhouse.insert_json_each_row("market_data.simulation_replay_staging", batch)
                    digest = sha256_file(path)
                    entry = {"path": str(relative), "feed": segment.feed, "segment": segment_index, "symbol": symbol,
                        "kind": kind, "rowCount": row_count, "compressedBytes": path.stat().st_size, "sha256": digest,
                        "apiParameters": {"symbols": symbol, "start": isoformat_z(segment.start), "end": isoformat_z(segment.end),
                            "feed": segment.feed, "sort": "asc", "limit": limit, "adjustment": "raw"}}
                    manifest["files"].append(entry); counts[kind] += row_count; counts["events"] += row_count; symbol_counts[kind] += row_count
                    if s3_prefix:
                        _upload_s3(path, f"{s3_prefix}/{relative.as_posix()}", sha256=digest, row_count=row_count)
                        if not local_only:
                            path.unlink()
        if int(counts["events"]) <= 0: raise RuntimeError("Alpaca returned no replay events")
        if clickhouse:
            _materialize_clickhouse(clickhouse)
            total = clickhouse.query_rows("SELECT count() events, countIf(event_type='trade') trades, countIf(event_type='quote') quotes "
                f"FROM market_data.simulation_replay_events WHERE dataset_id='{DATASET_ID}'")[0]
            if (int(total["events"]), int(total["trades"]), int(total["quotes"])) != (int(counts["events"]), int(counts["trades"]), int(counts["quotes"])):
                raise RuntimeError("ClickHouse total verification mismatch")
            rows = clickhouse.query_rows("SELECT symbol, countIf(event_type='trade') trades, countIf(event_type='quote') quotes "
                f"FROM market_data.simulation_replay_events WHERE dataset_id='{DATASET_ID}' GROUP BY symbol")
            actual = {str(row["symbol"]): {"trades": int(row["trades"]), "quotes": int(row["quotes"])} for row in rows}
            if actual != counts["bySymbol"]: raise RuntimeError("ClickHouse per-symbol verification mismatch")
        manifest["status"] = "LOCAL_READY" if local_only else "READY"; manifest["completedAt"] = datetime.now(UTC).isoformat()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        if s3_prefix: _upload_s3(manifest_path, f"{s3_prefix}/manifest.json", sha256=sha256_file(manifest_path))
        if clickhouse: _record_dataset_status(clickhouse, manifest)
        return manifest
    except Exception as exc:
        manifest.update(status="FAILED", error=str(exc)); manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        if clickhouse: _record_dataset_status(clickhouse, manifest)
        raise
    finally:
        if clickhouse: clickhouse.execute(f"ALTER TABLE market_data.simulation_replay_staging DELETE WHERE dataset_id='{DATASET_ID}' SETTINGS mutations_sync=1")


def _clear_clickhouse_build(client) -> None:
    for table in ("simulation_replay_staging", "simulation_replay_events", "simulation_replay_candles_1m"):
        client.execute(f"ALTER TABLE market_data.{table} DELETE WHERE dataset_id='{DATASET_ID}' SETTINGS mutations_sync=1")


def _materialize_clickhouse(client) -> None:
    client.execute("INSERT INTO market_data.simulation_replay_events (dataset_id,event_time,sequence,symbol,event_type,feed,payload) "
        "SELECT dataset_id,event_time,toUInt64(row_number() OVER (ORDER BY event_time,source_sequence)),symbol,event_type,feed,payload "
        f"FROM market_data.simulation_replay_staging WHERE dataset_id='{DATASET_ID}'")
    client.execute("INSERT INTO market_data.simulation_replay_candles_1m (dataset_id,event_time,symbol,open,high,low,close,volume,trade_count) "
        "SELECT dataset_id,toStartOfMinute(event_time),symbol,argMin(JSONExtractFloat(payload,'p'),tuple(event_time,source_sequence)),"
        "max(JSONExtractFloat(payload,'p')),min(JSONExtractFloat(payload,'p')),argMax(JSONExtractFloat(payload,'p'),tuple(event_time,source_sequence)),"
        f"sum(JSONExtractFloat(payload,'s')),count() FROM market_data.simulation_replay_staging WHERE dataset_id='{DATASET_ID}' AND event_type='trade' GROUP BY dataset_id,toStartOfMinute(event_time),symbol")


def _record_dataset_status(client, manifest: dict[str, object]) -> None:
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    client.insert_json_each_row("market_data.simulation_replay_datasets", [{"dataset_id": DATASET_ID,
        "status": manifest.get("status") or "FAILED", "start_time": manifest["startTime"], "end_time": manifest["endTimeExclusive"],
        "total_events": int(counts.get("events") or 0), "total_trades": int(counts.get("trades") or 0),
        "total_quotes": int(counts.get("quotes") or 0), "manifest": json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))}])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))


def _s3_manifest_exists(prefix: str) -> bool:
    from botocore.exceptions import ClientError
    bucket, key = _s3_bucket_key(f"{prefix}/manifest.json")
    try: _s3_client().head_object(Bucket=bucket, Key=key); return True
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}: return False
        raise


def _upload_s3(path: Path, uri: str, *, sha256: str, row_count: int | None = None) -> None:
    metadata = {"sha256": sha256}
    if row_count is not None: metadata["row-count"] = str(row_count)
    bucket, key = _s3_bucket_key(uri); client = _s3_client()
    client.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": metadata})
    head = client.head_object(Bucket=bucket, Key=key); remote = head.get("Metadata") or {}
    if int(head.get("ContentLength") or -1) != path.stat().st_size or remote.get("sha256") != sha256:
        raise RuntimeError(f"S3 verification failed for {uri}")
    if row_count is not None and remote.get("row-count") != str(row_count): raise RuntimeError(f"S3 row-count verification failed for {uri}")


def _s3_bucket_key(uri: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"): raise ValueError(f"invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def build_url(*, kind: str, base_url: str, symbol: str, feed: str, start: str, end: str, limit: int, page_token: str | None) -> str:
    params = {"symbols": symbol, "start": start, "end": end, "limit": str(limit), "feed": feed, "sort": "asc"}
    if kind == "bars": params.update(timeframe="1Min", adjustment="raw")
    if page_token: params["page_token"] = page_token
    return f"{base_url.rstrip('/')}/v2/stocks/{kind}?{urllib.parse.urlencode(params)}"


def fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response: return json.loads(response.read().decode())


def normalize_rows(kind: str, symbol: str, payload: dict[str, Any]) -> list[dict[str, object]]:
    message_type = {"bars": "b", "trades": "t", "quotes": "q"}[kind]
    return [{"T": message_type, "S": symbol, **row} for row in (payload.get(kind) or {}).get(symbol, []) if isinstance(row, dict)]


def _timestamp_in_half_open_window(value: object, start: str, end: str) -> bool:
    try: return _parse_iso_timestamp(start) <= _parse_iso_timestamp(value) < _parse_iso_timestamp(end)
    except ValueError: return False


def _parse_iso_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if not text: raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    if parsed.tzinfo is None: raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def selected_symbols(symbol_args: list[str], symbols_csv: str | None) -> list[str]:
    values = list(symbol_args) + ([item.strip() for item in symbols_csv.split(",")] if symbols_csv else [])
    if not any(item.strip() for item in values): raise ValueError("--symbol or --symbols is required")
    try: return normalize_symbols(values)
    except BadRequest as exc: raise ValueError(str(exc)) from exc


def selected_dates(*, date_arg: str | None, start_date_arg: str | None, end_date_arg: str | None,
                   days: int, include_weekends: bool, today: date) -> list[date]:
    if date_arg:
        if start_date_arg or end_date_arg: raise ValueError("--date cannot be combined with a range")
        return [date.fromisoformat(date_arg)]
    if days < 1: raise ValueError("--days must be at least 1")
    end = date.fromisoformat(end_date_arg) if end_date_arg else today
    start = date.fromisoformat(start_date_arg) if start_date_arg else end - timedelta(days=days - 1)
    values = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return values if include_weekends else [value for value in values if value.weekday() < 5]


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    body = "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows); path.write_text(body + ("\n" if body else ""))


def alpaca_headers() -> dict[str, str]:
    key, secret = first_env("APCA_API_KEY_ID", "ALPACA_API_KEY_ID"), first_env("APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY")
    if not key or not secret: raise SystemExit("Alpaca API credentials are required")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def first_env(*names: str) -> str | None:
    return next((os.getenv(name) for name in names if os.getenv(name)), None)


def default_replay_s3_uri() -> str | None:
    if os.getenv("SIM_REPLAY_S3_URI"): return os.getenv("SIM_REPLAY_S3_URI")
    return f"s3://{os.getenv('S3_BUCKET')}/simulator/replay/v1/dataset=sp500-top20-20260715-kst" if os.getenv("S3_BUCKET") else None


def utc_regular_open(value: date) -> str: return datetime.combine(value, time(13, 30), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
def utc_regular_close(value: date) -> str: return datetime.combine(value, time(20, 0), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
def file_name_for_kind(kind: str) -> str: return {"bars": "bars_1m.jsonl", "trades": "trades.jsonl", "quotes": "quotes.jsonl"}[kind]


if __name__ == "__main__": main()
