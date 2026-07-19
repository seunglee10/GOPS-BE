from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock
from typing import Any, Iterator

from gops_simul.config import PROJECT_ROOT
from gops_simul.dataset import (
    DATASET_END,
    DATASET_ID,
    DATASET_S3_PREFIX,
    DATASET_START,
    FEED_SEGMENTS,
    REPLAY_SYMBOLS,
    FeedSegment,
    dataset_manifest_template,
    isoformat_z,
)
from gops_simul.env import load_env_file
from gops_simul.errors import BadRequest
from gops_simul.storage import normalize_symbols


DATA_BASE_URL = "https://data.alpaca.markets"
DEFAULT_IMPORT_DAYS = 7
CLICKHOUSE_INSERT_BATCH_SIZE = 250_000
DEFAULT_IMPORT_WORKERS = 4
MATERIALIZE_CHUNK_MINUTES = 15
SOURCE_SEQUENCE_STRIDE = 1_000_000_000
ALPACA_MAX_ATTEMPTS = 9
ALPACA_RETRY_BASE_SECONDS = 0.5
ALPACA_RETRY_MAX_SECONDS = 30.0


class ClickHouseStagingWriter:
    """Combine rows from many symbol files into bounded ClickHouse inserts."""

    def __init__(self, client, *, batch_size: int = CLICKHOUSE_INSERT_BATCH_SIZE) -> None:
        self.client = client
        self.batch_size = max(1, int(batch_size))
        self._lock = Lock()
        self._batch: list[dict[str, object]] = []

    def add(self, row: dict[str, object]) -> None:
        pending: list[dict[str, object]] = []
        with self._lock:
            self._batch.append(row)
            if len(self._batch) >= self.batch_size:
                pending, self._batch = self._batch, []
        if pending:
            self.client.insert_json_each_row("market_data.simulation_replay_staging", pending)

    def flush(self) -> None:
        with self._lock:
            pending, self._batch = self._batch, []
        if pending:
            self.client.insert_json_each_row("market_data.simulation_replay_staging", pending)


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
    parser.add_argument(
        "--resume-from-s3",
        action="store_true",
        default=os.getenv("SIM_REPLAY_RESUME_FROM_S3", "").strip().lower() in {"1", "true", "yes"},
        help="restore already uploaded immutable source files from S3 instead of calling Alpaca again",
    )
    args = parser.parse_args(argv)

    if args.fixed_dataset:
        if not args.local_only and (not args.s3_uri or not args.clickhouse_url):
            raise SystemExit("--fixed-dataset requires --s3-uri and --clickhouse-url unless --local-only is set")
        if args.resume_from_s3 and args.local_only:
            raise SystemExit("--resume-from-s3 cannot be combined with --local-only")
        manifest = build_fixed_dataset(output_root=Path(args.data_root), base_url=args.base_url,
            headers={} if args.resume_from_s3 else alpaca_headers(), limit=args.limit, max_pages=args.max_pages, s3_uri=args.s3_uri,
            clickhouse_url=args.clickhouse_url, clickhouse_database=args.clickhouse_database,
            clickhouse_user=args.clickhouse_user, clickhouse_password=args.clickhouse_password,
            local_only=args.local_only, resume_from_s3=args.resume_from_s3)
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
                        local_only: bool, resume_from_s3: bool = False) -> dict[str, object]:
    root = output_root / DATASET_ID; manifest_path = root / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()).get("status") in {"READY", "LOCAL_READY"}:
        raise RuntimeError(f"immutable dataset already exists at {root}")
    root.mkdir(parents=True, exist_ok=True)
    s3_prefix = str(s3_uri or "").rstrip("/")
    if resume_from_s3 and not s3_prefix:
        raise RuntimeError("S3 URI is required to resume the fixed dataset import")
    if s3_prefix and _s3_manifest_exists(s3_prefix): raise RuntimeError(f"immutable S3 dataset already exists at {s3_prefix}")
    clickhouse = None
    if clickhouse_url:
        from gops_simul.clickhouse import ClickHouseHttpClient
        clickhouse = ClickHouseHttpClient(clickhouse_url, database=clickhouse_database, user=clickhouse_user,
            password=clickhouse_password,
            timeout_seconds=float(os.getenv("SIM_CLICKHOUSE_IMPORT_TIMEOUT_SECONDS", "7200")))
        _clear_clickhouse_build(clickhouse)
    staging_writer = ClickHouseStagingWriter(clickhouse) if clickhouse else None
    worker_count = max(1, min(16, int(os.getenv("SIM_IMPORT_WORKERS", str(DEFAULT_IMPORT_WORKERS)))))
    manifest = dataset_manifest_template(); manifest["createdAt"] = datetime.now(UTC).isoformat()
    manifest["source"] = {"provider": "alpaca", "baseUrl": base_url, "adjustment": "raw", "limit": limit,
        "importWorkers": worker_count, "restoredFromS3": resume_from_s3}
    counts = manifest["counts"]
    counts["bySymbol"] = {
        symbol: {"trades": 0, "quotes": 0}
        for symbol in REPLAY_SYMBOLS
    }
    completed_files: dict[str, int] = {symbol: 0 for symbol in REPLAY_SYMBOLS}
    error_symbols: set[str] = set()
    manifest["importResult"] = {
        "requestedSymbolCount": len(REPLAY_SYMBOLS),
        "successfulSymbolCount": 0,
        "storedRowCount": 0,
        "errorSymbols": [],
    }
    try:
        tasks: list[tuple[int, int, FeedSegment, str, str]] = []
        for segment_index, segment in enumerate(FEED_SEGMENTS, 1):
            for symbol in REPLAY_SYMBOLS:
                for kind in ("trades", "quotes"):
                    tasks.append((len(tasks), segment_index, segment, symbol, kind))
        stop_event = Event()
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="replay-import")
        collect_file = _restore_fixed_file_from_s3 if resume_from_s3 else _collect_fixed_file
        futures = {
            executor.submit(collect_file, root=root, file_ordinal=file_ordinal,
                segment_index=segment_index, segment=segment, symbol=symbol, kind=kind,
                base_url=base_url, headers=headers, limit=limit, max_pages=max_pages,
                s3_prefix=s3_prefix, staging_writer=staging_writer, local_only=local_only,
                stop_event=stop_event): (file_ordinal, symbol)
            for file_ordinal, segment_index, segment, symbol, kind in tasks
        }
        results: dict[int, tuple[dict[str, object], int]] = {}
        try:
            for future in as_completed(futures):
                file_ordinal, symbol = futures[future]
                try:
                    results[file_ordinal] = future.result()
                    completed_files[symbol] += 1
                except Exception:
                    error_symbols.add(symbol)
                    raise
        except Exception:
            stop_event.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if staging_writer:
            staging_writer.flush()
        for file_ordinal, _segment_index, _segment, _symbol, _kind in tasks:
            entry, row_count = results[file_ordinal]
            symbol = str(entry["symbol"]); kind = str(entry["kind"])
            symbol_counts = counts["bySymbol"][symbol]
            manifest["files"].append(entry); counts[kind] += row_count; counts["events"] += row_count; symbol_counts[kind] += row_count
        if int(counts["events"]) <= 0: raise RuntimeError("Alpaca returned no replay events")
        empty_symbols = {
            symbol
            for symbol, symbol_counts in counts["bySymbol"].items()
            if int(symbol_counts["trades"]) + int(symbol_counts["quotes"]) <= 0
        }
        if empty_symbols:
            error_symbols.update(empty_symbols)
            raise RuntimeError(f"Alpaca returned no replay events for: {','.join(sorted(empty_symbols))}")
        _update_import_result(manifest, counts, completed_files, error_symbols)
        if clickhouse:
            _materialize_clickhouse(clickhouse)
            total = clickhouse.query_rows("SELECT count() events, countIf(event_type='trade') trades, countIf(event_type='quote') quotes "
                f"FROM market_data.simulation_replay_events WHERE dataset_id='{DATASET_ID}'")[0]
            if (int(total["events"]), int(total["trades"]), int(total["quotes"])) != (int(counts["events"]), int(counts["trades"]), int(counts["quotes"])):
                raise RuntimeError("ClickHouse total verification mismatch")
            rows = clickhouse.query_rows("SELECT symbol, countIf(event_type='trade') trades, countIf(event_type='quote') quotes "
                f"FROM market_data.simulation_replay_events WHERE dataset_id='{DATASET_ID}' GROUP BY symbol")
            actual = {
                symbol: {"trades": 0, "quotes": 0}
                for symbol in REPLAY_SYMBOLS
            }
            actual.update({str(row["symbol"]): {"trades": int(row["trades"]), "quotes": int(row["quotes"])} for row in rows})
            if actual != counts["bySymbol"]: raise RuntimeError("ClickHouse per-symbol verification mismatch")
        manifest["status"] = "LOCAL_READY" if local_only else "READY"; manifest["completedAt"] = datetime.now(UTC).isoformat()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        if s3_prefix: _upload_s3(manifest_path, f"{s3_prefix}/manifest.json", sha256=sha256_file(manifest_path))
        if clickhouse: _record_dataset_status(clickhouse, manifest)
        return manifest
    except Exception as exc:
        _update_import_result(manifest, counts, completed_files, error_symbols)
        manifest.update(status="FAILED", error=str(exc)); manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        if clickhouse: _record_dataset_status(clickhouse, manifest)
        raise
    finally:
        if clickhouse: clickhouse.execute(f"ALTER TABLE market_data.simulation_replay_staging DELETE WHERE dataset_id='{DATASET_ID}' SETTINGS mutations_sync=1")


def deterministic_source_sequence(file_ordinal: int, row_number: int) -> int:
    if file_ordinal < 0:
        raise ValueError("file_ordinal must not be negative")
    if row_number < 1 or row_number >= SOURCE_SEQUENCE_STRIDE:
        raise ValueError(f"row_number must be between 1 and {SOURCE_SEQUENCE_STRIDE - 1}")
    return file_ordinal * SOURCE_SEQUENCE_STRIDE + row_number


def _collect_fixed_file(*, root: Path, file_ordinal: int, segment_index: int, segment: FeedSegment,
                        symbol: str, kind: str, base_url: str, headers: dict[str, str], limit: int,
                        max_pages: int | None, s3_prefix: str,
                        staging_writer: ClickHouseStagingWriter | None, local_only: bool,
                        stop_event: Event) -> tuple[dict[str, object], int]:
    relative = Path(f"feed={segment.feed}/segment={segment_index:02d}/symbol={symbol}/{kind}.jsonl.gz")
    path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for row_count, row in enumerate(iter_kind_rows(kind=kind, base_url=base_url, symbol=symbol,
            feed=segment.feed, start=isoformat_z(segment.start), end=isoformat_z(segment.end),
            limit=limit, max_pages=max_pages, headers=headers), 1):
            if stop_event.is_set():
                raise RuntimeError("replay import cancelled after another file failed")
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":")); handle.write(encoded + "\n")
            if staging_writer:
                staging_writer.add({"dataset_id": DATASET_ID, "event_time": row["t"], "source_file": str(relative),
                    "source_sequence": deterministic_source_sequence(file_ordinal, row_count), "symbol": symbol,
                    "event_type": "trade" if kind == "trades" else "quote", "feed": segment.feed, "payload": encoded})
            if row_count % 500_000 == 0:
                print(f"[{segment_index:02d}/{segment.feed}] {symbol} {kind}: {row_count:,} rows", flush=True)
    digest = sha256_file(path)
    entry = {"path": str(relative), "feed": segment.feed, "segment": segment_index, "symbol": symbol,
        "kind": kind, "rowCount": row_count, "compressedBytes": path.stat().st_size, "sha256": digest,
        "apiParameters": {"symbols": symbol, "start": isoformat_z(segment.start), "end": isoformat_z(segment.end),
            "feed": segment.feed, "sort": "asc", "limit": limit, "adjustment": "raw"}}
    if s3_prefix:
        _upload_s3(path, f"{s3_prefix}/{relative.as_posix()}", sha256=digest, row_count=row_count)
        if not local_only:
            path.unlink()
    print(f"[{segment_index:02d}/{segment.feed}] {symbol} {kind}: completed {row_count:,} rows", flush=True)
    return entry, row_count


def _restore_fixed_file_from_s3(*, root: Path, file_ordinal: int, segment_index: int,
                                segment: FeedSegment, symbol: str, kind: str,
                                base_url: str, headers: dict[str, str], limit: int,
                                max_pages: int | None, s3_prefix: str,
                                staging_writer: ClickHouseStagingWriter | None,
                                local_only: bool, stop_event: Event) -> tuple[dict[str, object], int]:
    del headers, max_pages
    relative = Path(f"feed={segment.feed}/segment={segment_index:02d}/symbol={symbol}/{kind}.jsonl.gz")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"{s3_prefix}/{relative.as_posix()}"
    bucket, key = _s3_bucket_key(uri)
    client = _s3_client()
    head = client.head_object(Bucket=bucket, Key=key)
    metadata = head.get("Metadata") or {}
    expected_sha256 = str(metadata.get("sha256") or "")
    expected_row_count = int(metadata.get("row-count") or -1)
    if not expected_sha256 or expected_row_count < 0:
        raise RuntimeError(f"S3 replay metadata is incomplete for {uri}")
    client.download_file(str(bucket), str(key), str(path), Config=_s3_transfer_config())
    if path.stat().st_size != int(head.get("ContentLength") or -1):
        raise RuntimeError(f"S3 replay size mismatch for {uri}")
    if sha256_file(path) != expected_sha256:
        raise RuntimeError(f"S3 replay checksum mismatch for {uri}")

    row_count = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for row_count, line in enumerate(handle, 1):
            if stop_event.is_set():
                raise RuntimeError("replay import cancelled after another file failed")
            encoded = line.rstrip("\n")
            row = json.loads(encoded)
            if staging_writer:
                staging_writer.add({"dataset_id": DATASET_ID, "event_time": row["t"], "source_file": str(relative),
                    "source_sequence": deterministic_source_sequence(file_ordinal, row_count), "symbol": symbol,
                    "event_type": "trade" if kind == "trades" else "quote", "feed": segment.feed, "payload": encoded})
    if row_count != expected_row_count:
        raise RuntimeError(f"S3 replay row-count mismatch for {uri}: expected {expected_row_count}, got {row_count}")
    entry = {"path": str(relative), "feed": segment.feed, "segment": segment_index, "symbol": symbol,
        "kind": kind, "rowCount": row_count, "compressedBytes": path.stat().st_size, "sha256": expected_sha256,
        "apiParameters": {"symbols": symbol, "start": isoformat_z(segment.start), "end": isoformat_z(segment.end),
            "feed": segment.feed, "sort": "asc", "limit": limit, "adjustment": "raw"}}
    if not local_only:
        path.unlink()
    print(f"[{segment_index:02d}/{segment.feed}] {symbol} {kind}: restored {row_count:,} rows from S3", flush=True)
    return entry, row_count


def _clear_clickhouse_build(client) -> None:
    for table in ("simulation_replay_staging", "simulation_replay_events", "simulation_replay_candles_1m"):
        client.execute(f"ALTER TABLE market_data.{table} DELETE WHERE dataset_id='{DATASET_ID}' SETTINGS mutations_sync=1")


def _materialize_clickhouse(client) -> None:
    sequence_offset = 0
    windows = list(_materialization_windows())
    window_counts: list[tuple[int, int]] = []
    for index, (start, end) in enumerate(windows, 1):
        bounds = _event_time_bounds(start, end)
        row = client.query_rows(
            "SELECT count() events, countIf(event_type='trade') trades "
            "FROM market_data.simulation_replay_staging "
            f"WHERE dataset_id='{DATASET_ID}' AND {bounds}"
        )[0]
        event_count = int(row["events"])
        trade_count = int(row["trades"])
        window_counts.append((event_count, trade_count))
        if event_count:
            client.execute(
                "INSERT INTO market_data.simulation_replay_events "
                "(dataset_id,event_time,sequence,symbol,event_type,feed,payload) "
                f"SELECT dataset_id,event_time,toUInt64({sequence_offset}) + "
                "toUInt64(row_number() OVER (ORDER BY event_time,source_sequence)),"
                "symbol,event_type,feed,payload "
                "FROM market_data.simulation_replay_staging "
                f"WHERE dataset_id='{DATASET_ID}' AND {bounds} "
                "SETTINGS max_threads=2,max_memory_usage=3500000000,"
                "max_bytes_before_external_sort=268435456"
            )
            sequence_offset += event_count
        print(f"materialized events {index}/{len(windows)}: {event_count:,} rows", flush=True)
    if sequence_offset <= 0:
        raise RuntimeError("ClickHouse staging contains no replay events to materialize")

    for index, ((start, end), (_event_count, trade_count)) in enumerate(zip(windows, window_counts), 1):
        if trade_count <= 0:
            print(f"materialized candles {index}/{len(windows)}: 0 trades", flush=True)
            continue
        bounds = _event_time_bounds(start, end)
        client.execute(
            "INSERT INTO market_data.simulation_replay_candles_1m "
            "(dataset_id,event_time,symbol,open,high,low,close,volume,trade_count) "
            "SELECT dataset_id,toStartOfMinute(event_time),symbol,"
            "argMin(JSONExtractFloat(payload,'p'),tuple(event_time,source_sequence)),"
            "max(JSONExtractFloat(payload,'p')),min(JSONExtractFloat(payload,'p')),"
            "argMax(JSONExtractFloat(payload,'p'),tuple(event_time,source_sequence)),"
            "sum(JSONExtractFloat(payload,'s')),count() "
            "FROM market_data.simulation_replay_staging "
            f"WHERE dataset_id='{DATASET_ID}' AND event_type='trade' AND {bounds} "
            "GROUP BY dataset_id,toStartOfMinute(event_time),symbol "
            "SETTINGS max_threads=2,max_memory_usage=3500000000,"
            "max_bytes_before_external_group_by=268435456"
        )
        print(f"materialized candles {index}/{len(windows)}: {trade_count:,} trades", flush=True)


def _materialization_windows() -> Iterator[tuple[datetime, datetime]]:
    cursor = DATASET_START
    chunk = timedelta(minutes=MATERIALIZE_CHUNK_MINUTES)
    while cursor < DATASET_END:
        end = min(DATASET_END, cursor + chunk)
        yield cursor, end
        cursor = end


def _event_time_bounds(start: datetime, end: datetime) -> str:
    return (
        f"event_time >= parseDateTime64BestEffort('{isoformat_z(start)}', 9) "
        f"AND event_time < parseDateTime64BestEffort('{isoformat_z(end)}', 9)"
    )


def _update_import_result(
    manifest: dict[str, object],
    counts: dict[str, object],
    completed_files: dict[str, int],
    error_symbols: set[str],
) -> None:
    expected_files_per_symbol = len(FEED_SEGMENTS) * 2
    manifest["importResult"] = {
        "requestedSymbolCount": len(REPLAY_SYMBOLS),
        "successfulSymbolCount": sum(
            completed == expected_files_per_symbol
            for completed in completed_files.values()
        ),
        "storedRowCount": int(counts.get("events") or 0),
        "errorSymbols": sorted(error_symbols),
    }


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


@lru_cache(maxsize=1)
def _s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        config=Config(
            retries={"max_attempts": 10, "mode": "adaptive"},
            max_pool_connections=16,
        ),
    )


@lru_cache(maxsize=1)
def _s3_transfer_config():
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(use_threads=False)


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
    client.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"Metadata": metadata},
        Config=_s3_transfer_config(),
    )
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
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, ALPACA_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise
            if attempt >= ALPACA_MAX_ATTEMPTS:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            delay = _retry_delay(attempt, retry_after)
            print(f"Alpaca HTTP {exc.code}; retrying in {delay:.1f}s ({attempt}/{ALPACA_MAX_ATTEMPTS})", flush=True)
            time_module.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            if attempt >= ALPACA_MAX_ATTEMPTS:
                raise
            delay = _retry_delay(attempt, None)
            print(f"Alpaca request failed; retrying in {delay:.1f}s ({attempt}/{ALPACA_MAX_ATTEMPTS})", flush=True)
            time_module.sleep(delay)
    raise RuntimeError("unreachable Alpaca retry state")


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    try:
        requested = float(retry_after) if retry_after is not None else 0.0
    except (TypeError, ValueError):
        requested = 0.0
    exponential = ALPACA_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1))
    return min(ALPACA_RETRY_MAX_SECONDS, max(requested, exponential))


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
    return f"s3://{os.getenv('S3_BUCKET')}/{DATASET_S3_PREFIX}" if os.getenv("S3_BUCKET") else None


def utc_regular_open(value: date) -> str: return datetime.combine(value, time(13, 30), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
def utc_regular_close(value: date) -> str: return datetime.combine(value, time(20, 0), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
def file_name_for_kind(kind: str) -> str: return {"bars": "bars_1m.jsonl", "trades": "trades.jsonl", "quotes": "quotes.jsonl"}[kind]


if __name__ == "__main__": main()
