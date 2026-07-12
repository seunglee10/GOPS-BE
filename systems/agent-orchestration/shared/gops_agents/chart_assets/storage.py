from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from threading import RLock, local
from typing import Any, Callable, Protocol

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alfaka.storage.clickhouse_loader import ClickHouseHttpClient


LOGGER = logging.getLogger(__name__)
ASSET_TABLE = "chart_analysis_assets"
POSTGRES_TABLE = "chart_assets.analysis_assets"
ALLOWED_STORAGE_MODES = {
    "clickhouse",
    "dual_clickhouse_read",
    "dual_postgres_read",
    "postgres",
}


class ChartAssetStore(Protocol):
    def save(self, asset: dict[str, Any]) -> bool | None: ...
    def get(self, symbol: str, interval: str) -> dict[str, Any] | None: ...
    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]: ...
    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]: ...
    def delete(self, symbols: list[str], intervals: list[str]) -> int: ...


class ClickHouseChartAssetStorage:
    def __init__(self, client: Any | None = None):
        self.client = client or ClickHouseHttpClient(
            os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            os.getenv("CLICKHOUSE_USER", "alfaka"),
            os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )
        self._save_locks_guard = RLock()
        self._save_locks: dict[tuple[str, str], RLock] = {}

    def save(self, asset: dict[str, Any]) -> bool:
        # The builder is a single replica but processes symbols concurrently.
        # Serialize compare-and-insert so a delayed older build cannot become
        # ClickHouse's argMax(inserted_at) winner inside this runtime.
        key = (str(asset["symbol"]).upper(), str(asset["interval"]))
        with self._save_lock_for(key):
            existing = self.get(str(asset["symbol"]), str(asset["interval"]))
            if existing is not None and not _asset_is_newer_or_changed(asset, existing):
                return False
            self.client.insert_json_each_row(ASSET_TABLE, [{
                "symbol": asset["symbol"],
                "interval": asset["interval"],
                "as_of": _clickhouse_datetime(asset["asOf"]),
                "generated_at": _clickhouse_datetime(asset["generatedAt"]),
                "asset_version": asset["assetVersion"],
                "kernel_version": asset["kernelVersion"],
                "prompt_version": asset.get("promptVersion") or "",
                "status": asset["status"],
                "payload": _canonical_payload(asset),
            }])
            return True

    def _save_lock_for(self, key: tuple[str, str]) -> RLock:
        with self._save_locks_guard:
            return self._save_locks.setdefault(key, RLock())

    def get(self, symbol: str, interval: str) -> dict[str, Any] | None:
        rows = self.client.query_json_each_row(
            """
            SELECT argMax(payload, inserted_at) AS payload
            FROM market_data.chart_analysis_assets
            WHERE symbol = {symbol:String} AND interval = {interval:String}
            HAVING count() > 0
            FORMAT JSONEachRow
            """,
            {"symbol": symbol.upper(), "interval": interval},
        )
        return _decode_asset(rows[0].get("payload")) if rows else None

    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]:
        rows = self.client.query_json_each_row(
            """
            SELECT interval, argMax(payload, inserted_at) AS payload
            FROM market_data.chart_analysis_assets
            WHERE symbol = {symbol:String}
            GROUP BY interval
            FORMAT JSONEachRow
            """,
            {"symbol": symbol.upper()},
        )
        return _assets_by_interval(rows)

    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        where = ""
        parameters: dict[str, Any] = {}
        if symbols:
            where = "WHERE symbol IN {symbols:Array(String)}"
            parameters["symbols"] = [symbol.upper() for symbol in symbols]
        rows = self.client.query_json_each_row(
            f"""
            SELECT symbol, interval,
                   formatDateTime(argMax(generated_at, inserted_at), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS generatedAt,
                   argMax(status, inserted_at) AS status,
                   argMax(asset_version, inserted_at) AS assetVersion,
                   length(argMax(payload, inserted_at)) AS payloadBytes,
                   JSONExtractString(argMax(payload, inserted_at), 'quality', 'state') AS qualityState,
                   JSONLength(JSONExtractRaw(argMax(payload, inserted_at), 'layers', 'structure'), 'drawings')
                     + JSONLength(JSONExtractRaw(argMax(payload, inserted_at), 'layers', 'trend'), 'drawings')
                     + JSONLength(JSONExtractRaw(argMax(payload, inserted_at), 'layers', 'agent'), 'drawings') AS drawingCount
            FROM market_data.chart_analysis_assets
            {where}
            GROUP BY symbol, interval
            ORDER BY symbol, interval
            FORMAT JSONEachRow
            """,
            parameters,
        )
        return [_coverage_row(row) for row in rows]

    def delete(self, symbols: list[str], intervals: list[str]) -> int:
        normalized_symbols, normalized_intervals = _normalize_selection(symbols, intervals)
        if not normalized_symbols or not normalized_intervals:
            return 0
        parameters = {"symbols": normalized_symbols, "intervals": normalized_intervals}
        rows = self.client.query_json_each_row(
            """
            SELECT uniqExact((symbol, interval)) AS assetCount
            FROM market_data.chart_analysis_assets
            WHERE symbol IN {symbols:Array(String)} AND interval IN {intervals:Array(String)}
            FORMAT JSONEachRow
            """,
            parameters,
        )
        asset_count = int(rows[0].get("assetCount") or 0) if rows else 0
        self.client.execute(
            """
            ALTER TABLE market_data.chart_analysis_assets
            DELETE WHERE symbol IN {symbols:Array(String)} AND interval IN {intervals:Array(String)}
            SETTINGS mutations_sync = 1
            """,
            parameters,
        )
        return asset_count

    def latest_assets(self) -> list[dict[str, Any]]:
        rows = self.client.query_json_each_row(
            """
            SELECT symbol, interval, argMax(payload, inserted_at) AS payload
            FROM market_data.chart_analysis_assets
            GROUP BY symbol, interval
            ORDER BY symbol, interval
            FORMAT JSONEachRow
            """
        )
        assets: list[dict[str, Any]] = []
        for row in rows:
            asset = _decode_asset(row.get("payload"))
            if asset is None:
                pair = f"{row.get('symbol') or '?'}:{row.get('interval') or '?'}"
                raise ValueError(f"invalid ClickHouse chart asset payload for {pair}")
            row_pair = (str(row.get("symbol") or "").upper(), str(row.get("interval") or ""))
            payload_pair = (str(asset.get("symbol") or "").upper(), str(asset.get("interval") or ""))
            if row_pair != payload_pair:
                raise ValueError(f"ClickHouse chart asset key/payload mismatch: {row_pair} != {payload_pair}")
            assets.append(asset)
        return assets


# Compatibility name retained for existing imports and tests. Environment-aware
# construction is intentionally explicit through build_chart_asset_storage_from_env.
class ChartAssetStorage(ClickHouseChartAssetStorage):
    pass


class PostgresChartAssetStorage:
    def __init__(
        self,
        conninfo: str | None = None,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.conninfo = conninfo or _database_conninfo()
        self._connector = connect or psycopg.connect

    def save(self, asset: dict[str, Any]) -> bool:
        payload = _canonical_payload(asset)
        projection = _asset_projection(asset, payload)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {POSTGRES_TABLE} (
                    symbol, "interval", as_of, generated_at, asset_version,
                    kernel_version, prompt_version, status, quality_state,
                    drawing_count, payload_bytes, asset_content_digest,
                    payload_digest, payload, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, now()
                )
                ON CONFLICT (symbol, "interval") DO UPDATE SET
                    as_of = EXCLUDED.as_of,
                    generated_at = EXCLUDED.generated_at,
                    asset_version = EXCLUDED.asset_version,
                    kernel_version = EXCLUDED.kernel_version,
                    prompt_version = EXCLUDED.prompt_version,
                    status = EXCLUDED.status,
                    quality_state = EXCLUDED.quality_state,
                    drawing_count = EXCLUDED.drawing_count,
                    payload_bytes = EXCLUDED.payload_bytes,
                    asset_content_digest = EXCLUDED.asset_content_digest,
                    payload_digest = EXCLUDED.payload_digest,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                WHERE EXCLUDED.generated_at > {POSTGRES_TABLE}.generated_at
                   OR (
                       EXCLUDED.generated_at = {POSTGRES_TABLE}.generated_at
                       AND EXCLUDED.payload_digest IS DISTINCT FROM {POSTGRES_TABLE}.payload_digest
                   )
                """,
                (
                    projection["symbol"], projection["interval"], projection["as_of"],
                    projection["generated_at"], projection["asset_version"],
                    projection["kernel_version"], projection["prompt_version"],
                    projection["status"], projection["quality_state"],
                    projection["drawing_count"], projection["payload_bytes"],
                    projection["asset_content_digest"], projection["payload_digest"],
                    Jsonb(asset),
                ),
            )
            conn.commit()
            return int(cursor.rowcount) > 0

    def get(self, symbol: str, interval: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload FROM {POSTGRES_TABLE} WHERE symbol = %s AND \"interval\" = %s",
                (symbol.upper(), interval),
            ).fetchone()
        return _decode_asset(row.get("payload")) if row else None

    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT \"interval\" AS \"interval\", payload FROM {POSTGRES_TABLE} WHERE symbol = %s ORDER BY \"interval\"",
                (symbol.upper(),),
            ).fetchall()
        return _assets_by_interval(rows)

    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        parameters: tuple[Any, ...] = ()
        where = ""
        if symbols:
            where = "WHERE symbol = ANY(%s)"
            parameters = ([symbol.upper() for symbol in symbols],)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, "interval" AS "interval", generated_at AS "generatedAt", status,
                       asset_version AS "assetVersion", quality_state AS "qualityState",
                       payload_bytes AS "payloadBytes", drawing_count AS "drawingCount"
                FROM {POSTGRES_TABLE}
                {where}
                ORDER BY symbol, "interval"
                """,
                parameters,
            ).fetchall()
        return [_coverage_row(row) for row in rows]

    def delete(self, symbols: list[str], intervals: list[str]) -> int:
        normalized_symbols, normalized_intervals = _normalize_selection(symbols, intervals)
        if not normalized_symbols or not normalized_intervals:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                DELETE FROM {POSTGRES_TABLE}
                WHERE symbol = ANY(%s) AND "interval" = ANY(%s)
                RETURNING symbol, "interval"
                """,
                (normalized_symbols, normalized_intervals),
            ).fetchall()
            conn.commit()
        return len(rows)

    def payload_digests(self) -> dict[tuple[str, str], str]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT symbol, \"interval\" AS \"interval\", payload FROM {POSTGRES_TABLE} ORDER BY symbol, \"interval\""
            ).fetchall()
        result: dict[tuple[str, str], str] = {}
        for row in rows:
            key = (str(row["symbol"]), str(row["interval"]))
            asset = _decode_asset(row.get("payload"))
            result[key] = _payload_digest(_canonical_payload(asset)) if asset is not None else "invalid_payload"
        return result

    def all_pairs(self) -> set[tuple[str, str]]:
        return set(self.payload_digests())

    def delete_pairs(self, pairs: set[tuple[str, str]]) -> int:
        if not pairs:
            return 0
        deleted = 0
        with self._connect() as conn:
            for symbol, interval in sorted(pairs):
                row = conn.execute(
                    f"DELETE FROM {POSTGRES_TABLE} WHERE symbol = %s AND \"interval\" = %s RETURNING symbol",
                    (symbol, interval),
                ).fetchone()
                deleted += int(row is not None)
            conn.commit()
        return deleted

    def _connect(self) -> Any:
        return self._connector(self.conninfo, row_factory=dict_row)


class DualChartAssetStorage:
    """Read from one store while shadow-writing the other during migration."""

    def __init__(self, primary: ChartAssetStore, shadow: ChartAssetStore, *, mode: str) -> None:
        self.primary = primary
        self.shadow = shadow
        self.mode = mode
        self._shadow_state = local()

    def save(self, asset: dict[str, Any]) -> bool:
        primary_result = self.primary.save(asset)
        try:
            shadow_result = self.shadow.save(asset)
            if primary_result is False or shadow_result is False:
                if not _stores_have_same_asset(self.primary, self.shadow, asset):
                    raise RuntimeError("chart asset dual-store monotonic write diverged")
        except Exception as exc:  # primary remains authoritative in a dual mode
            warning = f"chart_asset_shadow_write_failed:{self.mode}:{exc.__class__.__name__}"
            warnings = self._shadow_warning_buffer()
            warnings.append(warning)
            del warnings[:-32]
            LOGGER.warning(warning, exc_info=True)
        return primary_result is not False

    def get(self, symbol: str, interval: str) -> dict[str, Any] | None:
        return self.primary.get(symbol, interval)

    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]:
        return self.primary.get_symbol_assets(symbol)

    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return self.primary.coverage(symbols)

    def delete(self, symbols: list[str], intervals: list[str]) -> int:
        primary_result: int | None = None
        errors: list[Exception] = []
        try:
            primary_result = self.primary.delete(symbols, intervals)
        except Exception as exc:
            errors.append(exc)
        try:
            self.shadow.delete(symbols, intervals)
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError("chart asset dual-store delete did not complete in both stores") from errors[0]
        return int(primary_result or 0)

    def pop_warnings(self) -> list[str]:
        buffer = self._shadow_warning_buffer()
        warnings = list(buffer)
        buffer.clear()
        return warnings

    def _shadow_warning_buffer(self) -> list[str]:
        if not hasattr(self._shadow_state, "warnings"):
            self._shadow_state.warnings = []
        return self._shadow_state.warnings


class MaintenanceChartAssetStorage:
    """Keep serving reads while preventing queued workers from writing during cutover."""

    def __init__(self, delegate: ChartAssetStore) -> None:
        self.delegate = delegate

    def save(self, _asset: dict[str, Any]) -> bool:
        raise RuntimeError("chart asset storage is read-only during migration maintenance")

    def get(self, symbol: str, interval: str) -> dict[str, Any] | None:
        return self.delegate.get(symbol, interval)

    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]:
        return self.delegate.get_symbol_assets(symbol)

    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return self.delegate.coverage(symbols)

    def delete(self, _symbols: list[str], _intervals: list[str]) -> int:
        raise RuntimeError("chart asset storage is read-only during migration maintenance")


def build_chart_asset_storage_from_env() -> ChartAssetStore:
    mode = os.getenv("CHART_ASSET_STORAGE_MODE", "clickhouse").strip().lower()
    if mode not in ALLOWED_STORAGE_MODES:
        raise ValueError(f"unsupported CHART_ASSET_STORAGE_MODE: {mode}")
    clickhouse = ClickHouseChartAssetStorage()
    if mode == "clickhouse":
        storage: ChartAssetStore = clickhouse
    else:
        postgres = PostgresChartAssetStorage()
        if mode == "postgres":
            storage = postgres
        elif mode == "dual_clickhouse_read":
            storage = DualChartAssetStorage(clickhouse, postgres, mode=mode)
        else:
            storage = DualChartAssetStorage(postgres, clickhouse, mode=mode)
    if _storage_maintenance_enabled():
        return MaintenanceChartAssetStorage(storage)
    return storage


def _storage_maintenance_enabled() -> bool:
    return os.getenv("CHART_ASSET_STORAGE_MAINTENANCE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _database_conninfo() -> str:
    conninfo = os.getenv("DATABASE_URL")
    if conninfo:
        return conninfo
    required = ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"chart asset PostgreSQL settings missing: {','.join(missing)}")
    return make_conninfo(
        host=os.environ["DATABASE_HOST"],
        port=os.getenv("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
    )


def _asset_projection(asset: dict[str, Any], payload: str | None = None) -> dict[str, Any]:
    payload = payload or _canonical_payload(asset)
    build = asset.get("build") if isinstance(asset.get("build"), dict) else {}
    quality = asset.get("quality") if isinstance(asset.get("quality"), dict) else {}
    return {
        "symbol": str(asset["symbol"]).upper(),
        "interval": str(asset["interval"]),
        "as_of": _postgres_datetime(asset["asOf"]),
        "generated_at": _postgres_datetime(asset["generatedAt"]),
        "asset_version": str(asset.get("assetVersion") or ""),
        "kernel_version": str(asset.get("kernelVersion") or ""),
        "prompt_version": str(asset.get("promptVersion") or ""),
        "status": str(asset.get("status") or ""),
        "quality_state": str(quality.get("state") or "") or None,
        "drawing_count": _drawing_count(asset),
        "payload_bytes": len(payload.encode("utf-8")),
        "asset_content_digest": str(build.get("assetContentDigest") or "") or None,
        "payload_digest": _payload_digest(payload),
    }


def _assets_by_interval(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    assets: dict[str, dict[str, Any] | None] = {
        interval: None for interval in ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M")
    }
    for row in rows:
        interval = str(row.get("interval") or "")
        if interval in assets:
            assets[interval] = _decode_asset(row.get("payload"))
    return assets


def _coverage_row(row: dict[str, Any]) -> dict[str, Any]:
    drawing_count = int(row.get("drawingCount") or 0)
    return {
        "symbol": str(row.get("symbol") or ""),
        "interval": str(row.get("interval") or ""),
        "generatedAt": _iso_timestamp(row.get("generatedAt")),
        "status": str(row.get("status") or ""),
        "assetVersion": str(row.get("assetVersion") or ""),
        "qualityState": str(row.get("qualityState") or "") or None,
        "payloadBytes": int(row.get("payloadBytes") or 0),
        "drawingCount": drawing_count,
        "storedDrawingCount": drawing_count,
        "freshness": "unknown",
        "staleByBars": None,
    }


def _normalize_selection(symbols: list[str], intervals: list[str]) -> tuple[list[str], list[str]]:
    return (
        list(dict.fromkeys(symbol.upper() for symbol in symbols)),
        list(dict.fromkeys(intervals)),
    )


def _drawing_count(asset: dict[str, Any]) -> int:
    layers = asset.get("layers") if isinstance(asset.get("layers"), dict) else {}
    return sum(
        len((layers.get(name) or {}).get("drawings") or [])
        for name in ("structure", "trend", "agent")
    )


def _canonical_payload(asset: dict[str, Any]) -> str:
    return json.dumps(asset, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _payload_digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _asset_is_newer_or_changed(incoming: dict[str, Any], existing: dict[str, Any]) -> bool:
    try:
        incoming_at = _postgres_datetime(incoming["generatedAt"])
        existing_at = _postgres_datetime(existing["generatedAt"])
    except (KeyError, TypeError, ValueError):
        return True
    if incoming_at != existing_at:
        return incoming_at > existing_at
    return _payload_digest(_canonical_payload(incoming)) != _payload_digest(_canonical_payload(existing))


def _stores_have_same_asset(
    primary: ChartAssetStore,
    shadow: ChartAssetStore,
    asset: dict[str, Any],
) -> bool:
    symbol = str(asset["symbol"])
    interval = str(asset["interval"])
    left = primary.get(symbol, interval)
    right = shadow.get(symbol, interval)
    if left is None or right is None:
        return left is right
    return _payload_digest(_canonical_payload(left)) == _payload_digest(_canonical_payload(right))


def _decode_asset(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _clickhouse_datetime(value: Any) -> str:
    parsed = _postgres_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _postgres_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(value or "")
