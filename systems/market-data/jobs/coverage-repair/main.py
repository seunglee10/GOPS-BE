"""Audit chart coverage through the API and queue missing backfills."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_INTERVALS = ("1m", "5m", "10m", "1D", "1W", "1M")
def main() -> None:
    base_url = os.getenv("GOPS_API_BASE_URL", "http://gops-backend:8000").rstrip("/")
    symbols = parse_symbols(os.getenv("COVERAGE_REPAIR_SYMBOLS") or os.getenv("ALPACA_SYMBOLS"))
    intervals = parse_csv(os.getenv("COVERAGE_REPAIR_INTERVALS")) or list(DEFAULT_INTERVALS)
    force = os.getenv("COVERAGE_REPAIR_FORCE", "false").lower() in {"1", "true", "yes"}
    dry_run = os.getenv("COVERAGE_REPAIR_DRY_RUN", "false").lower() in {"1", "true", "yes"}

    if not symbols:
        raise SystemExit("COVERAGE_REPAIR_SYMBOLS or ALPACA_SYMBOLS is required.")

    report = []
    queued = 0
    failures = 0
    for symbol in symbols:
        for interval in intervals:
            status = fetch_snapshot_status(base_url, symbol, interval)
            should_repair = not is_renderable(status)
            action = "ok"
            backfill = None
            if should_repair and status["canBackfill"]:
                action = "would_queue" if dry_run else "queued"
                if not dry_run:
                    backfill = request_backfill(base_url, symbol, interval, force=force)
                    queued += 1
            elif should_repair:
                action = "needs_attention"
                failures += 1
            report.append({**status, "action": action, "backfill": backfill})

    print(json.dumps({"queued": queued, "failures": failures, "items": report}, ensure_ascii=False, indent=2), flush=True)
    if failures:
        raise SystemExit(1)


def fetch_snapshot_status(base_url: str, symbol: str, interval: str) -> dict[str, object]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": "200"})
    payload = request_json("GET", f"{base_url}/api/charts/candles?{params}")
    return {
        "symbol": symbol,
        "interval": interval,
        "sourceInterval": payload.get("sourceInterval"),
        "dataStatus": payload.get("dataStatus"),
        "backfillStatus": payload.get("backfillStatus"),
        "canBackfill": bool(payload.get("canBackfill")),
        "candleCount": len(payload.get("candles") or []),
        "coverageState": (payload.get("coverage") or {}).get("state"),
        "coverageReason": (payload.get("coverage") or {}).get("reasonCode"),
        "coverageRenderable": (payload.get("coverage") or {}).get("renderable"),
    }


def request_backfill(base_url: str, symbol: str, interval: str, *, force: bool) -> dict[str, object]:
    body = json.dumps({"symbol": symbol, "interval": interval, "force": force}).encode("utf-8")
    return request_json("POST", f"{base_url}/api/charts/backfill", body=body)


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


if __name__ == "__main__":
    main()
