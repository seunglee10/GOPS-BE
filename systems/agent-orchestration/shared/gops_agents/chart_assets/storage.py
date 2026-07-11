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
                   JSONExtractString(argMax(payload, inserted_at), 'quality', 'state') AS qualityState
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
            "freshness": "unknown",
            "staleByBars": None,
        } for row in rows]

    def is_fresh(self, symbol: str, interval: str, hours: int) -> bool:
        if hours <= 0:
            return False
        asset = self.get(symbol, interval)
        if not asset:
            return False
        try:
            generated = datetime.fromisoformat(str(asset["generatedAt"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return False
        return (datetime.now(timezone.utc) - generated).total_seconds() < hours * 3600


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
