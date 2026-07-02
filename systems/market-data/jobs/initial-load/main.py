"""Plan and enqueue chunked initial market-data loads."""

from __future__ import annotations

import json
import math
import os
import sys

from alfaka.alpaca.subscription import configured_collection_symbols
from alfaka.backfill.status import RedisBackfillStore, chunk_backfill_range, initial_load_range_guard, queue_backlog_count
from alfaka.backfill.status import initial_load_chunk_days, parse_time
from alfaka.backfill.status import validate_initial_load_range
from alfaka.serving.intervals import normalize_chart_interval


DEFAULT_INTERVALS = ("1D",)


def main() -> None:
    symbols, symbol_source = resolve_initial_load_symbols(os.getenv("INITIAL_LOAD_SYMBOLS"))
    intervals = parse_csv(os.getenv("INITIAL_LOAD_INTERVALS")) or list(DEFAULT_INTERVALS)
    start = os.getenv("INITIAL_LOAD_START")
    end = os.getenv("INITIAL_LOAD_END")
    dry_run = os.getenv("INITIAL_LOAD_DRY_RUN", "true").lower() in {"1", "true", "yes"}
    force = os.getenv("INITIAL_LOAD_FORCE", "false").lower() in {"1", "true", "yes"}
    source_preference = os.getenv("INITIAL_LOAD_SOURCE_PREFERENCE", "coverage-first")
    max_enqueued = optional_int(os.getenv("INITIAL_LOAD_MAX_ENQUEUE"))
    max_backlog = optional_int(os.getenv("INITIAL_LOAD_MAX_BACKLOG"))

    if not symbols:
        raise SystemExit("INITIAL_LOAD_SYMBOLS or ALPACA_SYMBOLS is required.")
    if not start or not end:
        raise SystemExit("INITIAL_LOAD_START and INITIAL_LOAD_END are required.")

    report = plan_initial_load(
        RedisBackfillStore(),
        symbols=symbols,
        intervals=intervals,
        start=start,
        end=end,
        symbol_source=symbol_source,
        dry_run=dry_run,
        force=force,
        source_preference=source_preference,
        max_enqueued=max_enqueued,
        max_backlog=max_backlog,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if report["failures"]:
        raise SystemExit(1)


def plan_initial_load(
    store,
    *,
    symbols,
    intervals,
    start,
    end,
    symbol_source="explicit",
    dry_run=True,
    force=False,
    source_preference="coverage-first",
    max_enqueued=None,
    max_backlog=None,
):
    items = []
    failures = 0
    for interval_value in intervals:
        try:
            interval = normalize_chart_interval(interval_value)
            if interval not in {"1m", "1D"}:
                raise ValueError(f"Initial Load supports canonical source intervals only: {interval}")
            validate_initial_load_range(interval, start, end)
            chunks = chunk_backfill_range(start, end, interval)
            plan = preload_plan_summary(
                symbols=symbols,
                symbol_source=symbol_source,
                interval=interval,
                start=start,
                end=end,
                chunks=chunks,
                force=force,
                source_preference=source_preference,
                max_enqueued=max_enqueued,
                max_backlog=max_backlog,
            )
            if dry_run:
                backlog, queue_metrics_error = dry_run_queue_backlog(store)
                result = {
                    "jobType": "initial_load",
                    "interval": interval,
                    "symbolCount": len(symbols),
                    "chunkCount": len(chunks) * len(symbols),
                    "createdCount": 0,
                    "backlogBefore": backlog,
                    "dryRun": True,
                    "throttled": False,
                    "requests": [],
                    **plan,
                }
                if queue_metrics_error:
                    result["queueMetricsError"] = queue_metrics_error
            else:
                result = store.create_initial_load_requests(
                    symbols,
                    interval,
                    start,
                    end,
                    max_enqueued=max_enqueued,
                    max_backlog=max_backlog,
                    source_preference=source_preference,
                    force=force,
                )
                result["dryRun"] = False
                result.update(plan)
            items.append(result)
        except Exception as exc:
            failures += 1
            items.append({
                "jobType": "initial_load",
                "interval": interval_value,
                "error": str(exc),
                "dryRun": dry_run,
            })
    return {
        "dryRun": dry_run,
        "symbolCount": len(symbols),
        "symbolSource": symbol_source,
        "intervals": [str(interval) for interval in intervals],
        "failures": failures,
        "items": items,
    }


def parse_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def parse_symbols(value: str | None) -> list[str]:
    return [item.upper() for item in parse_csv(value)]


def resolve_initial_load_symbols(value: str | None) -> tuple[list[str], str]:
    raw_value = (value or "").strip()
    allowed_symbols = configured_collection_symbols()
    allowed = set(allowed_symbols)
    if not raw_value or raw_value.lower() in {"universe", "all", "*"}:
        return allowed_symbols, "configured_collection"
    symbols = []
    outside = []
    seen = set()
    for symbol in parse_symbols(raw_value):
        if allowed and symbol not in allowed:
            outside.append(symbol)
            continue
        if symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    if outside:
        raise ValueError(f"INITIAL_LOAD_SYMBOLS includes symbols outside configured universe: {', '.join(sorted(set(outside)))}")
    return symbols, "explicit"


def optional_int(value: str | None) -> int | None:
    return int(value) if value not in {None, ""} else None


def dry_run_queue_backlog(store):
    try:
        return queue_backlog_count(store.queue_metrics()), None
    except Exception as exc:
        return None, str(exc)


def preload_plan_summary(
    *,
    symbols,
    symbol_source,
    interval,
    start,
    end,
    chunks,
    force,
    source_preference,
    max_enqueued,
    max_backlog,
):
    estimate = estimate_preload_size(
        symbol_count=len(symbols),
        interval=interval,
        start=start,
        end=end,
        chunks_per_symbol=len(chunks),
    )
    return {
        "symbolSource": symbol_source,
        "sourceInterval": interval,
        "range": {"start": chunks[0].start if chunks else start, "end": chunks[-1].end if chunks else end},
        "chunkDays": initial_load_chunk_days(interval),
        "chunksPerSymbol": len(chunks),
        "chunkPreview": chunk_preview(chunks),
        "estimate": estimate,
        "resume": {
            "strategy": "idempotent_chunk_requests",
            "idempotencyKey": "jobType+symbol+sourceInterval+chunkStart+chunkEnd",
            "force": bool(force),
            "sourcePreference": source_preference,
            "maxEnqueue": max_enqueued,
            "maxBacklog": max_backlog,
        },
        "s3Validation": {
            "requiredBeforeRealPreload": [
                "manifest_metadata",
                "row_count",
                "object_key_layout",
                "idempotent_resume",
            ]
        },
        "rangeGuard": initial_load_range_guard(interval),
    }


def estimate_preload_size(symbol_count, interval, start, end, chunks_per_symbol):
    rows_per_symbol = estimate_rows_per_symbol(interval, start, end)
    source_objects = symbol_count * chunks_per_symbol
    raw_partition_mode = os.getenv("S3_HISTORICAL_RAW_PARTITION_MODE", "chunk").strip().lower()
    processed_manifest_layout = os.getenv("S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT", "compact").strip().lower()
    raw_objects = source_objects if raw_partition_mode in {"chunk", "request", "compact"} else symbol_count * estimate_trading_days(start, end)
    processed_objects = source_objects
    return {
        "method": "configured_minutes_per_trading_day_estimate",
        "rowsPerSymbol": rows_per_symbol,
        "totalRows": rows_per_symbol * symbol_count,
        "rawObjectPartitionMode": "chunk" if raw_partition_mode in {"chunk", "request", "compact"} else "daily",
        "processedManifestLayout": "compact" if processed_manifest_layout == "compact" else "daily",
        "rawObjects": raw_objects,
        "processedObjects": processed_objects,
        "manifestEntries": source_objects + processed_objects,
    }


def estimate_rows_per_symbol(interval, start, end):
    trading_days = estimate_trading_days(start, end)
    if normalize_chart_interval(interval) == "1D":
        return trading_days
    minutes_per_trading_day = int(os.getenv("HISTORICAL_1M_MINUTES_PER_TRADING_DAY", "960"))
    return int(math.ceil(trading_days * minutes_per_trading_day))


def estimate_trading_days(start, end):
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    calendar_days = max((end_dt - start_dt).total_seconds() / 86400, 0)
    return int(math.ceil(calendar_days * 252 / 365.25))


def chunk_preview(chunks, limit=3):
    preview = [{"start": chunk.start, "end": chunk.end} for chunk in chunks[:limit]]
    if len(chunks) > limit:
        preview.append({"ellipsis": True, "remaining": len(chunks) - limit})
        preview.append({"start": chunks[-1].start, "end": chunks[-1].end})
    return preview


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"initial-load failed: {exc}", file=sys.stderr, flush=True)
        raise
