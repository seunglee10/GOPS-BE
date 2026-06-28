from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any


def fetch_alpaca_assets(
    api_key: str,
    api_secret: str,
    *,
    base_url: str | None = None,
    asset_class: str = "us_equity",
    status: str = "active",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    endpoint = f"{(base_url or os.getenv('ALPACA_TRADING_BASE_URL', 'https://paper-api.alpaca.markets')).rstrip('/')}/v2/assets"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params = {}
    if asset_class:
        params["asset_class"] = asset_class
    if status:
        params["status"] = status

    import requests

    response = requests.get(endpoint, headers=headers, params=params, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Alpaca assets request failed: status={response.status_code}, body={response.text}")

    payload = response.json()
    assets = payload.get("assets", payload) if isinstance(payload, dict) else payload
    if not isinstance(assets, list):
        raise RuntimeError("Alpaca assets response is not a list")

    selected_assets = assets[:limit] if limit else assets
    return [asset_to_symbol_metadata(asset) for asset in selected_assets if asset.get("symbol")]


def asset_to_symbol_metadata(asset: dict[str, Any], *, updated_at: str | None = None) -> dict[str, Any]:
    symbol = str(asset.get("symbol", "")).upper()
    asset_class = asset.get("asset_class") or asset.get("class") or "us_equity"
    updated_at = updated_at or datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "eventType": "SYMBOL_METADATA",
        "symbol": symbol,
        "name": asset.get("name") or symbol,
        "exchange": asset.get("exchange"),
        "market": asset.get("market") or infer_market(asset.get("exchange")),
        "assetClass": asset_class,
        "tradable": bool(asset.get("tradable", True)),
        "status": asset.get("status") or "unknown",
        "source": "alpaca",
        "updatedAt": updated_at,
        "raw": asset,
    }


def infer_market(exchange):
    if not exchange:
        return "US"
    return "US" if str(exchange).upper() in {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "OTC"} else str(exchange).upper()
