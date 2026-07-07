"""Audit chart coverage through the API on-demand fill path."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M")
def main() -> None:
    base_url = os.getenv("GOPS_API_BASE_URL", "http://gops-backend:8000").rstrip("/")
    symbols = parse_symbols(os.getenv("COVERAGE_REPAIR_SYMBOLS") or os.getenv("ALPACA_SYMBOLS"))
    intervals = parse_csv(os.getenv("COVERAGE_REPAIR_INTERVALS")) or list(DEFAULT_INTERVALS)
    dry_run = os.getenv("COVERAGE_REPAIR_DRY_RUN", "false").lower() in {"1", "true", "yes"}

    if not symbols:
        raise SystemExit("COVERAGE_REPAIR_SYMBOLS or ALPACA_SYMBOLS is required.")

    report = []
    failures = 0
    for symbol in symbols:
        for interval in intervals:
            status = fetch_snapshot_status(base_url, symbol, interval)
            should_repair = not is_renderable(status)
            action = "ok" if not should_repair else "needs_attention"
            repair_ranges = recommended_repair_ranges(status)
            if should_repair:
                fill_status = (status.get("fill") or {}).get("status") if isinstance(status.get("fill"), dict) else None
                if fill_status in {"filled", "partial", "timeout", "empty", "failed"}:
                    action = f"fill_{fill_status}"
                failures += 1
            report.append({**status, "action": action, "repairRanges": repair_ranges})

    print(json.dumps({"dryRun": dry_run, "failures": failures, "items": report}, ensure_ascii=False, indent=2), flush=True)
    if failures and not dry_run:
        raise SystemExit(1)


def fetch_snapshot_status(base_url: str, symbol: str, interval: str) -> dict[str, object]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": "200"})
    payload = request_json("GET", f"{base_url}/api/charts/candles?{params}")
    return {
        "symbol": symbol,
        "interval": interval,
        "sourceInterval": payload.get("sourceInterval"),
        "dataStatus": payload.get("dataStatus"),
        "candleCount": len(payload.get("candles") or []),
        "coverageState": (payload.get("coverage") or {}).get("state"),
        "coverageReason": (payload.get("coverage") or {}).get("reasonCode"),
        "coverageRenderable": (payload.get("coverage") or {}).get("renderable"),
        "repairStatus": (payload.get("coverage") or {}).get("repairStatus"),
        "fill": payload.get("fill"),
        "targetRangeFrom": (payload.get("coverage") or {}).get("targetRangeFrom") or payload.get("targetRangeFrom"),
        "availableFrom": (payload.get("coverage") or {}).get("availableFrom") or payload.get("availableFrom"),
        "availableTo": (payload.get("coverage") or {}).get("availableTo") or payload.get("availableTo"),
    }


def request_json(method: str, url: str, body: bytes | None = None) -> dict[str, object]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"{method} {url} failed: status={exc.code} body={detail}", file=sys.stderr, flush=True)
        raise


def parse_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def parse_symbols(value: str | None) -> list[str]:
    return [item.upper() for item in parse_csv(value)]


def is_renderable(status: dict[str, object]) -> bool:
    if status["candleCount"] <= 0:
        return False
    if status["dataStatus"] == "ready":
        return True
    return status["dataStatus"] == "partial" and status.get("coverageRenderable") is not False


def recommended_repair_ranges(status: dict[str, object]) -> list[dict[str, str | None]]:
    target_from = status.get("targetRangeFrom")
    available_from = status.get("availableFrom")
    if isinstance(target_from, str) and isinstance(available_from, str) and target_from < available_from:
        return [{"start": target_from, "end": available_from}]
    if isinstance(target_from, str) and not available_from:
        return [{"start": target_from, "end": None}]
    return [{"start": None, "end": None}]


if __name__ == "__main__":
    main()
