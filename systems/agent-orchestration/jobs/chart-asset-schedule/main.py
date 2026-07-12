from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from gops_agents.chart_assets.envelope import ALLOWED_INTERVALS, ChartAssetBuildEnvelope
from gops_agents.chart_assets.job_store import PostgresChartAssetJobStore
from gops_agents.query_understanding.supported_companies import load_market_registry_symbols


def main() -> int:
    symbols = _csv(os.getenv("CHART_ASSET_SYMBOLS"))
    if not symbols:
        symbols, _source, _version = load_market_registry_symbols()
    intervals = tuple(_csv(os.getenv("CHART_ASSET_INTERVALS"))) or ALLOWED_INTERVALS
    invalid = set(intervals).difference(ALLOWED_INTERVALS)
    if not symbols:
        raise RuntimeError("S&P 500 symbol registry is empty")
    if invalid:
        raise ValueError(f"Unsupported chart asset intervals: {sorted(invalid)}")
    scheduled = os.getenv("CHART_ASSET_SCHEDULED", "false").lower() in {"1", "true", "yes", "on"}
    job_id = os.getenv("CHART_ASSET_JOB_ID")
    if scheduled and not job_id:
        job_id = f"cab-scheduled-{datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()}"
    envelope = ChartAssetBuildEnvelope.create(
        requested_by=os.getenv("CHART_ASSET_REQUESTED_BY", "kubernetes-job"),
        symbols=symbols, intervals=intervals, force=_bool("CHART_ASSET_FORCE"), job_id=job_id,
    )
    PostgresChartAssetJobStore().enqueue(envelope)
    print(json.dumps({"jobId": envelope.job_id, "symbols": len(symbols), "intervals": list(intervals)}, sort_keys=True))
    return 0


def _csv(value: str | None) -> list[str]:
    return list(dict.fromkeys(item.strip().upper() for item in (value or "").split(",") if item.strip()))


def _bool(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
