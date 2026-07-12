from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


POSTGRES_TABLE = "chart_assets.geometry_assets"
ASSET_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")


class ChartAssetStore(Protocol):
    def save(self, asset: dict[str, Any]) -> bool: ...
    def get(self, symbol: str, interval: str) -> dict[str, Any] | None: ...
    def get_symbol_assets(self, symbol: str) -> dict[str, dict[str, Any] | None]: ...
    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]: ...
    def delete(self, symbols: list[str], intervals: list[str]) -> int: ...


class PostgresChartAssetStorage:
    def __init__(self, conninfo: str | None = None, *, connect: Callable[..., Any] | None = None) -> None:
        self.conninfo = conninfo or _database_conninfo()
        self._connector = connect or psycopg.connect

    def save(self, asset: dict[str, Any]) -> bool:
        if asset.get("assetVersion") != "geometry":
            raise ValueError("PostgreSQL geometry store accepts only assetVersion=geometry")
        _validate_asset_identity(asset)
        payload = _canonical_payload(asset)
        projection = _asset_projection(asset, payload)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {POSTGRES_TABLE} (
                    symbol, "interval", as_of, generated_at, asset_version,
                    algorithm_version, status, coverage_state, drawing_count,
                    payload_bytes, input_digest, payload_digest, payload, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (symbol, "interval") DO UPDATE SET
                    as_of = EXCLUDED.as_of,
                    generated_at = EXCLUDED.generated_at,
                    asset_version = EXCLUDED.asset_version,
                    algorithm_version = EXCLUDED.algorithm_version,
                    status = EXCLUDED.status,
                    coverage_state = EXCLUDED.coverage_state,
                    drawing_count = EXCLUDED.drawing_count,
                    payload_bytes = EXCLUDED.payload_bytes,
                    input_digest = EXCLUDED.input_digest,
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
                    projection["symbol"], projection["interval"], projection["as_of"], projection["generated_at"],
                    projection["asset_version"], projection["algorithm_version"], projection["status"],
                    projection["coverage_state"], projection["drawing_count"], projection["payload_bytes"],
                    projection["input_digest"], projection["payload_digest"], Jsonb(asset),
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
                f"SELECT \"interval\", payload FROM {POSTGRES_TABLE} WHERE symbol = %s ORDER BY \"interval\"",
                (symbol.upper(),),
            ).fetchall()
        result = {interval: None for interval in ASSET_INTERVALS}
        for row in rows:
            interval = str(row.get("interval") or "")
            if interval in result:
                result[interval] = _decode_asset(row.get("payload"))
        return result

    def coverage(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        where = ""
        parameters: tuple[Any, ...] = ()
        if symbols:
            where = "WHERE symbol = ANY(%s)"
            parameters = ([symbol.upper() for symbol in symbols],)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, "interval", generated_at, status, asset_version,
                       coverage_state, payload_bytes, drawing_count
                FROM {POSTGRES_TABLE} {where}
                ORDER BY symbol, "interval"
                """, parameters,
            ).fetchall()
        return [{
            "symbol": row["symbol"], "interval": row["interval"], "generatedAt": _iso(row["generated_at"]),
            "status": row["status"], "assetVersion": row["asset_version"], "coverageState": row["coverage_state"],
            "payloadBytes": int(row["payload_bytes"]), "drawingCount": int(row["drawing_count"]),
            "storedDrawingCount": int(row["drawing_count"]), "freshness": "unknown", "staleByBars": None,
        } for row in rows]

    def delete(self, symbols: list[str], intervals: list[str]) -> int:
        normalized_symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        normalized_intervals = list(dict.fromkeys(intervals))
        if not normalized_symbols or not normalized_intervals:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                f"DELETE FROM {POSTGRES_TABLE} WHERE symbol = ANY(%s) AND \"interval\" = ANY(%s) RETURNING symbol",
                (normalized_symbols, normalized_intervals),
            ).fetchall()
            conn.commit()
        return len(rows)

    def _connect(self) -> Any:
        return self._connector(self.conninfo, row_factory=dict_row)


class MaintenanceChartAssetStorage:
    def __init__(self, delegate: ChartAssetStore) -> None:
        self.delegate = delegate
    def save(self, _asset): raise RuntimeError("chart asset storage is read-only during migration maintenance")
    def get(self, symbol, interval): return self.delegate.get(symbol, interval)
    def get_symbol_assets(self, symbol): return self.delegate.get_symbol_assets(symbol)
    def coverage(self, symbols=None): return self.delegate.coverage(symbols)
    def delete(self, _symbols, _intervals): raise RuntimeError("chart asset storage is read-only during migration maintenance")


def build_chart_asset_storage_from_env() -> ChartAssetStore:
    storage: ChartAssetStore = PostgresChartAssetStorage()
    return MaintenanceChartAssetStorage(storage) if _storage_maintenance_enabled() else storage


def _storage_maintenance_enabled() -> bool:
    return os.getenv("CHART_ASSET_STORAGE_MAINTENANCE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _database_conninfo() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    required = ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"chart asset PostgreSQL settings missing: {','.join(missing)}")
    return make_conninfo(
        host=os.environ["DATABASE_HOST"], port=os.getenv("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"], user=os.environ["DATABASE_USER"], password=os.environ["DATABASE_PASSWORD"],
    )


def _asset_projection(asset: dict[str, Any], payload: str | None = None) -> dict[str, Any]:
    payload = payload or _canonical_payload(asset)
    coverage = asset.get("coverage") or {}
    geometry = asset.get("geometry") or {}
    return {
        "symbol": str(asset["symbol"]).upper(), "interval": str(asset["interval"]),
        "as_of": _timestamp(asset["asOf"]), "generated_at": _timestamp(asset["generatedAt"]),
        "asset_version": str(asset.get("assetVersion") or ""), "algorithm_version": str(asset.get("algorithmVersion") or ""),
        "status": str(asset.get("status") or ""), "coverage_state": str(coverage.get("state") or ""),
        "drawing_count": len(geometry.get("drawings") or []), "payload_bytes": len(payload.encode("utf-8")),
        "input_digest": str(asset.get("inputDigest") or ""), "payload_digest": _payload_digest(payload),
    }


def _canonical_payload(asset: dict[str, Any]) -> str:
    return json.dumps(asset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _validate_asset_identity(asset: dict[str, Any]) -> None:
    symbol = str(asset.get("symbol") or "").strip().upper()
    interval = str(asset.get("interval") or "")
    if not symbol or interval not in ASSET_INTERVALS or asset.get("sourceInterval") != interval:
        raise ValueError("Geometry asset symbol and interval identity is invalid")
    drawings = (asset.get("geometry") or {}).get("drawings") or []
    if len(drawings) > 6:
        raise ValueError("Geometry asset drawing limit exceeded")
    if any(
        str(drawing.get("symbol") or "").strip().upper() != symbol
        or drawing.get("interval") != interval
        or drawing.get("sourceInterval") != interval
        for drawing in drawings
    ):
        raise ValueError("Geometry drawing identity does not match its asset")


def _payload_digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_asset(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict): return dict(value)
    try: decoded = json.loads(str(value))
    except (TypeError, ValueError): return None
    return decoded if isinstance(decoded, dict) else None


def _timestamp(value: Any) -> Any:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _iso(value: Any) -> str:
    if isinstance(value, str): return value
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
