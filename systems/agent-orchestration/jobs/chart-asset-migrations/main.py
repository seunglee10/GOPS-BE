"""One-shot schema, latest-row sync, and parity verification for chart assets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg

from gops_agents.chart_assets.storage import (
    ClickHouseChartAssetStorage,
    PostgresChartAssetStorage,
    _canonical_payload,
    _database_conninfo,
    _payload_digest,
)


SQL_PATH = Path(__file__).with_name("001_create_chart_assets.sql")


def apply_schema(conninfo: str | None = None) -> None:
    with psycopg.connect(conninfo or _database_conninfo()) as conn:
        conn.execute(SQL_PATH.read_text(encoding="utf-8"))
        conn.commit()


def sync_latest(
    clickhouse: ClickHouseChartAssetStorage | None = None,
    postgres: PostgresChartAssetStorage | None = None,
    *,
    prune: bool = False,
) -> dict[str, Any]:
    source = clickhouse or ClickHouseChartAssetStorage()
    target = postgres or PostgresChartAssetStorage()
    assets = source.latest_assets()
    source_digests = {
        (str(asset["symbol"]).upper(), str(asset["interval"])): _payload_digest(_canonical_payload(asset))
        for asset in assets
    }
    written = sum(target.save(asset) is not False for asset in assets)
    extras = target.all_pairs().difference(source_digests)
    pruned = target.delete_pairs(extras) if prune else 0
    parity = verify_parity(source_digests, target.payload_digests())
    return {
        "sourceRows": len(source_digests),
        "attemptedRows": len(assets),
        "upsertedRows": written,
        "extraRows": len(extras),
        "prunedRows": pruned,
        **parity,
    }


def verify_current(
    clickhouse: ClickHouseChartAssetStorage | None = None,
    postgres: PostgresChartAssetStorage | None = None,
) -> dict[str, Any]:
    source = clickhouse or ClickHouseChartAssetStorage()
    target = postgres or PostgresChartAssetStorage()
    source_digests = {
        (str(asset["symbol"]).upper(), str(asset["interval"])): _payload_digest(_canonical_payload(asset))
        for asset in source.latest_assets()
    }
    return verify_parity(source_digests, target.payload_digests())


def verify_parity(
    source: dict[tuple[str, str], str],
    target: dict[tuple[str, str], str],
) -> dict[str, Any]:
    missing = sorted(set(source).difference(target))
    extra = sorted(set(target).difference(source))
    mismatched = sorted(key for key in set(source).intersection(target) if source[key] != target[key])
    return {
        "matchedRows": len(set(source).intersection(target)) - len(mismatched),
        "missingRows": len(missing),
        "extraRows": len(extra),
        "digestMismatches": len(mismatched),
        "parity": not missing and not extra and not mismatched,
        "sampleMissing": [list(item) for item in missing[:10]],
        "sampleExtra": [list(item) for item in extra[:10]],
        "sampleMismatched": [list(item) for item in mismatched[:10]],
    }


def main() -> int:
    action = os.getenv("CHART_ASSET_MIGRATION_ACTION", "migrate").strip().lower()
    if action not in {"migrate", "sync", "verify"}:
        raise ValueError(f"unsupported CHART_ASSET_MIGRATION_ACTION: {action}")
    if action in {"sync", "verify"} and not _maintenance_enabled():
        print(json.dumps({
            "action": action,
            "error": "chart_asset_storage_maintenance_required",
            "parity": False,
        }, ensure_ascii=False, sort_keys=True))
        return 3
    apply_schema()
    if action == "migrate":
        result: dict[str, Any] = {"schemaApplied": True}
    elif action == "sync":
        prune = os.getenv("CHART_ASSET_MIGRATION_PRUNE", "false").strip().lower() in {"1", "true", "yes", "on"}
        result = sync_latest(prune=prune)
    else:
        result = verify_current()
    print(json.dumps({"action": action, **result}, ensure_ascii=False, sort_keys=True))
    if action in {"sync", "verify"} and not result.get("parity"):
        return 2
    return 0


def _maintenance_enabled() -> bool:
    return os.getenv("CHART_ASSET_STORAGE_MAINTENANCE", "false").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
