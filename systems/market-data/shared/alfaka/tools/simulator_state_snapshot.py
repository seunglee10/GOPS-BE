from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable

from alfaka.common.redis_keys import RedisKeyBuilder
from alfaka.tools.cleanup_simulator_state import (
    DEFAULT_CANDLE_INTERVALS,
    DEFAULT_SIMULATOR_SYMBOLS,
    cleanup_simulator_market_state,
    simulator_market_state_keys,
)


SNAPSHOT_VERSION = 1
SNAPSHOT_MANIFEST_SUFFIX = "simulator:snapshot:manifest"
SNAPSHOT_DATA_SUFFIX = "simulator:snapshot:data"


def capture_simulator_market_state(
    redis_client,
    *,
    symbols: Iterable[str] = DEFAULT_SIMULATOR_SYMBOLS,
    intervals: Iterable[str] = DEFAULT_CANDLE_INTERVALS,
    prefix: str | None = None,
) -> int:
    normalized_symbols = _normalized_values(symbols, uppercase=True)
    normalized_intervals = _normalized_values(intervals)
    keys = RedisKeyBuilder(prefix)
    manifest_key = keys.key(SNAPSHOT_MANIFEST_SUFFIX)
    backup_prefix = keys.key(SNAPSHOT_DATA_SUFFIX)
    _delete_previous_snapshot(redis_client, manifest_key, backup_prefix)

    entries: list[dict[str, str]] = []
    source_keys = simulator_market_state_keys(
        redis_client,
        symbols=normalized_symbols,
        intervals=normalized_intervals,
        prefix=prefix,
    )
    for source_key in sorted(source_keys):
        if not redis_client.exists(source_key):
            continue
        backup_key = f"{backup_prefix}:{hashlib.sha256(source_key.encode('utf-8')).hexdigest()}"
        if not redis_client.copy(source_key, backup_key, replace=True):
            raise RuntimeError(f"Could not snapshot Redis key: {source_key}")
        entries.append({"source": source_key, "backup": backup_key})

    redis_client.set(
        manifest_key,
        json.dumps(
            {
                "version": SNAPSHOT_VERSION,
                "symbols": normalized_symbols,
                "intervals": normalized_intervals,
                "entries": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    return len(entries)


def restore_simulator_market_state(
    redis_client,
    *,
    symbols: Iterable[str] = DEFAULT_SIMULATOR_SYMBOLS,
    intervals: Iterable[str] = DEFAULT_CANDLE_INTERVALS,
    prefix: str | None = None,
) -> int:
    normalized_symbols = _normalized_values(symbols, uppercase=True)
    normalized_intervals = _normalized_values(intervals)
    keys = RedisKeyBuilder(prefix)
    manifest_key = keys.key(SNAPSHOT_MANIFEST_SUFFIX)
    backup_prefix = keys.key(SNAPSHOT_DATA_SUFFIX)
    entries = _snapshot_entries(redis_client.get(manifest_key), keys.prefix, backup_prefix)

    cleanup_simulator_market_state(
        redis_client,
        symbols=normalized_symbols,
        intervals=normalized_intervals,
        prefix=prefix,
    )

    restored = 0
    for entry in entries:
        if redis_client.copy(entry["backup"], entry["source"], replace=True):
            restored += 1
    return restored


def _delete_previous_snapshot(redis_client, manifest_key: str, backup_prefix: str) -> None:
    stale_backup_keys = set(redis_client.scan_iter(match=f"{backup_prefix}:*"))
    stale_backup_keys.add(manifest_key)
    redis_client.delete(*sorted(stale_backup_keys))


def _snapshot_entries(raw_manifest, redis_prefix: str, backup_prefix: str) -> list[dict[str, str]]:
    if isinstance(raw_manifest, bytes):
        raw_manifest = raw_manifest.decode("utf-8")
    if not isinstance(raw_manifest, str) or not raw_manifest:
        return []
    try:
        manifest = json.loads(raw_manifest)
    except (TypeError, ValueError):
        return []
    if manifest.get("version") != SNAPSHOT_VERSION or not isinstance(manifest.get("entries"), list):
        return []

    source_prefix = f"{redis_prefix}:" if redis_prefix else ""
    entries: list[dict[str, str]] = []
    for item in manifest["entries"]:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        backup = item.get("backup")
        if not isinstance(source, str) or not isinstance(backup, str):
            continue
        if not source.startswith(source_prefix) or not backup.startswith(f"{backup_prefix}:"):
            continue
        entries.append({"source": source, "backup": backup})
    return entries


def _normalized_values(values: Iterable[str], *, uppercase: bool = False) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = str(value).strip()
        if uppercase:
            item = item.upper()
        if item and item not in normalized:
            normalized.append(item)
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture or restore the pre-simulator GOPS Redis market state.")
    parser.add_argument("action", choices=("capture", "restore"))
    parser.add_argument("--symbols", default=",".join(DEFAULT_SIMULATOR_SYMBOLS))
    parser.add_argument("--intervals", default=",".join(DEFAULT_CANDLE_INTERVALS))
    args = parser.parse_args()

    import redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    callback = capture_simulator_market_state if args.action == "capture" else restore_simulator_market_state
    changed = callback(client, symbols=args.symbols.split(","), intervals=args.intervals.split(","))
    verb = "Captured" if args.action == "capture" else "Restored"
    print(f"{verb} {changed} pre-simulation Redis keys.")


if __name__ == "__main__":
    main()
