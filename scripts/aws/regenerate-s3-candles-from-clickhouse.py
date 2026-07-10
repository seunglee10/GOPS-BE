#!/usr/bin/env python3
"""Regenerate S3 final candle parquet and manifests from ClickHouse.

This is a recovery tool for cases where objects under S3_FINAL_PREFIX or
S3_MANIFEST_PREFIX were deleted but ClickHouse still has canonical candle rows.
Run it inside a market-storage/backend pod, or from an environment that has the
alfaka package, ClickHouse HTTP access, and AWS S3 credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from datetime import datetime, timezone
from typing import Any

from alfaka.common.s3_client import create_s3_client
from alfaka.storage.clickhouse_loader import (
    ClickHouseHttpClient,
    clickhouse_string_literal,
)
from alfaka.storage.processed_s3_sink import flush_buffer
from alfaka.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX


def main() -> None:
    args = parse_args()
    bucket = os.getenv("S3_BUCKET")
    final_prefix = os.getenv("S3_FINAL_PREFIX", "market-data/rebuild-20260702-lazy-v1/final").strip("/")
    manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX).strip("/")
    output_format = args.output_format or os.getenv("S3_PROCESSED_FORMAT", "parquet")
    manifest_layout = args.manifest_layout or os.getenv("S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT", "compact")

    if not bucket:
        raise SystemExit("S3_BUCKET is required.")
    if output_format not in {"parquet", "jsonl"}:
        raise SystemExit("output format must be parquet or jsonl.")

    client = ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://clickhouse:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
    )
    s3 = None if args.dry_run else create_s3_client()
    run_id = args.run_id or f"s3-recovery-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    groups = select_candle_groups(client, args)
    if args.limit_groups:
        groups = groups[: args.limit_groups]

    summary = {
        "dryRun": args.dry_run,
        "bucket": bucket,
        "finalPrefix": final_prefix,
        "manifestPrefix": manifest_prefix,
        "outputFormat": output_format,
        "manifestLayout": manifest_layout,
        "runId": run_id,
        "groupCount": len(groups),
        "sourceRowCount": sum(int(group["row_count"]) for group in groups),
        "writtenObjectCount": 0,
        "writtenRowCount": 0,
    }
    print(json.dumps({"status": "planned", **summary}, ensure_ascii=False), flush=True)
    if args.dry_run and not args.verify_rows:
        print(json.dumps({"status": "complete", **summary}, ensure_ascii=False), flush=True)
        return

    for index, group in enumerate(groups, start=1):
        rows = select_candle_rows(client, group, args)
        if not rows:
            continue
        partition_key = recovery_partition_key(final_prefix, group, run_id)
        item = {
            "index": index,
            "symbol": group["symbol"],
            "interval": group["interval"],
            "feed": group["feed"],
            "day": group["day"],
            "rowCount": len(rows),
            "partitionKey": partition_key,
        }
        if not args.dry_run:
            if args.verbose_flush:
                object_key = flush_recovery_buffer(
                    s3,
                    bucket,
                    partition_key,
                    rows,
                    output_format,
                    manifest_prefix,
                    manifest_layout,
                    args.force,
                )
            else:
                with contextlib.redirect_stdout(io.StringIO()):
                    object_key = flush_recovery_buffer(
                        s3,
                        bucket,
                        partition_key,
                        rows,
                        output_format,
                        manifest_prefix,
                        manifest_layout,
                        args.force,
                    )
            item["objectKey"] = object_key
            summary["writtenObjectCount"] += 1
            summary["writtenRowCount"] += len(rows)
        if args.include_groups:
            summary.setdefault("groups", []).append(item)
        if args.progress_every and index % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "status": "progress",
                        "processedGroups": index,
                        "totalGroups": len(groups),
                        "writtenObjectCount": summary["writtenObjectCount"],
                        "writtenRowCount": summary["writtenRowCount"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    print(json.dumps({"status": "complete", **summary}, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Plan work without writing S3 objects.")
    parser.add_argument("--symbol", action="append", default=[], help="Limit to one symbol. Repeatable.")
    parser.add_argument("--interval", action="append", default=[], help="Limit to one chart interval. Repeatable.")
    parser.add_argument("--start", help="Inclusive UTC start timestamp, e.g. 2026-07-01T00:00:00Z.")
    parser.add_argument("--end", help="Exclusive UTC end timestamp, e.g. 2026-07-11T00:00:00Z.")
    parser.add_argument("--price-adjustment", default="split", help="ClickHouse price_adjustment filter.")
    parser.add_argument("--canonical-version", default="v2", help="ClickHouse canonical_version filter.")
    parser.add_argument("--limit-groups", type=int, default=0, help="Limit number of symbol/interval/day groups.")
    parser.add_argument("--run-id", help="Stable recovery id for partition keys.")
    parser.add_argument("--force", action="store_true", help="Write revision keys even when deterministic keys exist.")
    parser.add_argument("--output-format", choices=["parquet", "jsonl"], help="S3 final object format.")
    parser.add_argument("--manifest-layout", choices=["compact", "daily"], help="Manifest layout.")
    parser.add_argument("--progress-every", type=int, default=100, help="Progress print interval in groups.")
    parser.add_argument("--verify-rows", action="store_true", help="In dry-run mode, query each group's rows too.")
    parser.add_argument("--include-groups", action="store_true", help="Include per-group details in final JSON output.")
    parser.add_argument("--verbose-flush", action="store_true", help="Print per-object flush_buffer logs.")
    return parser.parse_args()


def select_candle_groups(client: ClickHouseHttpClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    where_sql = candle_where_sql(args)
    query = f"""
        SELECT
            symbol,
            interval,
            feed,
            feed_profile,
            toString(toDate(event_time)) AS day,
            count() AS row_count,
            min(event_time) AS min_event_time,
            max(event_time) AS max_event_time
        FROM market_data.chart_candles FINAL
        WHERE {where_sql}
        GROUP BY symbol, interval, feed, feed_profile, day
        ORDER BY interval, symbol, day, feed_profile
        FORMAT JSONEachRow
    """
    return client.query_json_each_row(query)


def select_candle_rows(client: ClickHouseHttpClient, group: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    where_sql = " AND ".join(
        [
            candle_where_sql(args),
            f"symbol = {clickhouse_string_literal(group['symbol'])}",
            f"interval = {clickhouse_string_literal(group['interval'])}",
            f"feed = {clickhouse_string_literal(group['feed'])}",
            f"feed_profile = {clickhouse_string_literal(group['feed_profile'])}",
            f"toDate(event_time) = toDate({clickhouse_string_literal(group['day'])})",
        ]
    )
    query = f"""
        SELECT
            'CANDLE' AS eventType,
            symbol,
            interval,
            formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
            open,
            high,
            low,
            close,
            volume,
            trade_count AS tradeCount,
            vwap,
            ma5,
            ma20,
            ma60,
            is_closed AS isClosed,
            correction_type AS correctionType,
            source,
            feed,
            feed_profile AS feedProfile,
            market_session AS marketSession,
            price_adjustment AS priceAdjustment,
            canonical_version AS canonicalVersion,
            source_event_id AS sourceEventId,
            if(isNull(created_at), NULL, formatDateTime(created_at, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC')) AS createdAt
        FROM market_data.chart_candles FINAL
        WHERE {where_sql}
        ORDER BY event_time
        FORMAT JSONEachRow
    """
    return client.query_json_each_row(query)


def candle_where_sql(args: argparse.Namespace) -> str:
    filters = [
        "is_closed = 1",
        f"price_adjustment = {clickhouse_string_literal(args.price_adjustment)}",
        f"canonical_version = {clickhouse_string_literal(args.canonical_version)}",
    ]
    if args.symbol:
        filters.append("symbol IN (" + ",".join(clickhouse_string_literal(symbol.upper()) for symbol in args.symbol) + ")")
    if args.interval:
        filters.append("interval IN (" + ",".join(clickhouse_string_literal(interval) for interval in args.interval) + ")")
    if args.start:
        filters.append(f"event_time >= parseDateTime64BestEffort({clickhouse_string_literal(args.start)}, 3, 'UTC')")
    if args.end:
        filters.append(f"event_time < parseDateTime64BestEffort({clickhouse_string_literal(args.end)}, 3, 'UTC')")
    return " AND ".join(filters)


def recovery_partition_key(final_prefix: str, group: dict[str, Any], run_id: str) -> str:
    year, month, day = str(group["day"]).split("-")
    feed = group.get("feed") or group.get("feed_profile") or "unknown"
    return (
        f"{final_prefix}/candles/feed={feed}/interval={group['interval']}/symbol={group['symbol']}"
        f"/year={year}/month={month}/day={day}/backfill_request={run_id}"
    )


def flush_recovery_buffer(
    s3: Any,
    bucket: str,
    partition_key: str,
    rows: list[dict[str, Any]],
    output_format: str,
    manifest_prefix: str,
    manifest_layout: str,
    force: bool,
) -> str:
    return flush_buffer(
        s3,
        bucket,
        partition_key,
        rows,
        output_format,
        manifest_prefix=manifest_prefix,
        manifest_layout=manifest_layout,
        force=force,
    )


if __name__ == "__main__":
    main()
