from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from alfaka.storage.clickhouse_loader import ClickHouseHttpClient


ASSET_TABLE = "chart_analysis_assets"


class ChartAssetStorage:
    def __init__(self, client: Any | None = None):
        self.client = client or ClickHouseHttpClient(
            os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            os.getenv("CLICKHOUSE_USER", "alfaka"),
            os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )

    def save(self, asset: dict[str, Any]) -> None:
        self.client.insert_json_each_row(ASSET_TABLE, [{
            "symbol": asset["symbol"],
            "interval": asset["interval"],
            "as_of": _clickhouse_datetime(asset["asOf"]),
            "generated_at": _clickhouse_datetime(asset["generatedAt"]),
            "asset_version": asset["assetVersion"],
            "kernel_version": asset["kernelVersion"],
            "prompt_version": asset.get("promptVersion") or "",
            "status": asset["status"],
            "payload": json.dumps(asset, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        }])

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
        assets: dict[str, dict[str, Any] | None] = {"1D": None, "1W": None, "1M": None}
        for row in rows:
            interval = str(row.get("interval") or "")
            if interval in assets:
                assets[interval] = _decode_asset(row.get("payload"))
        return assets

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
        return [{
            "symbol": str(row.get("symbol") or ""),
            "interval": str(row.get("interval") or ""),
            "generatedAt": str(row.get("generatedAt") or ""),
            "status": str(row.get("status") or ""),
            "assetVersion": str(row.get("assetVersion") or ""),
            "qualityState": str(row.get("qualityState") or "") or None,
            "payloadBytes": int(row.get("payloadBytes") or 0),
            "drawingCount": int(row.get("drawingCount") or 0),
            "freshness": "unknown",
            "staleByBars": None,
        } for row in rows]

    def delete(self, symbols: list[str], intervals: list[str]) -> int:
        normalized_symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        normalized_intervals = list(dict.fromkeys(intervals))
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
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
