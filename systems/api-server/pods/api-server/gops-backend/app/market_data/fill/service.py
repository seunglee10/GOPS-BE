from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from alfaka.backfill.runner import (
    BackfillUnavailable,
    fetch_alpaca_bars,
    find_processed_candle_objects,
    historical_feed_for_symbol,
    raw_bars_to_processed_candles,
    repair_daily_bar_outliers,
)
from alfaka.serving.intervals import (
    minimum_renderable_returned_bars,
    minimum_renderable_source_bars,
    normalize_chart_interval,
    source_interval_for,
)
from alfaka.serving.provider import requested_window_for_interval
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, should_ensure_schema_on_start
from alfaka.storage.processed_s3_sink import flush_buffer
from alfaka.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX
from alfaka.storage.s3_materializer import materialize_s3_processed_objects
from app.market_data.backfill.service import renderability_payload


FILL_TIMEOUT_SECONDS = 30.0
BACKGROUND_FILL_TIMEOUT_SECONDS = 60.0
FILL_SOURCES = ("redis", "clickhouse", "s3", "alpaca")
logger = logging.getLogger(__name__)

_BACKGROUND_EXECUTOR: ThreadPoolExecutor | None = None
_BACKGROUND_ACTIVE: set[str] = set()
_BACKGROUND_LOCK = threading.Lock()


class OnDemandFillService:
    """Queues historical repair for only the frontend-requested chart window."""

    def __init__(
        self,
        provider=None,
        s3=None,
        clickhouse_client=None,
        timeout_seconds: float | None = None,
        background_enabled: bool | None = None,
        background_executor: ThreadPoolExecutor | None = None,
    ):
        self.provider = provider
        self.s3 = s3
        self.clickhouse_client = clickhouse_client
        configured_timeout = os.getenv(
            "ON_DEMAND_FILL_BACKGROUND_TIMEOUT_SECONDS",
            os.getenv("ON_DEMAND_FILL_TIMEOUT_SECONDS", BACKGROUND_FILL_TIMEOUT_SECONDS),
        )
        self.timeout_seconds = float(timeout_seconds or configured_timeout)
        self.background_enabled = (
            background_enabled
            if background_enabled is not None
            else os.getenv("ON_DEMAND_FILL_BACKGROUND_ENABLED", "true").lower() not in {"0", "false", "off", "no"}
        )
        self.background_executor = background_executor

    def fill_if_needed(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        before: str | None,
        from_time: str | None,
        to_time: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        interval = normalize_chart_interval(interval)
        source_interval = source_interval_for(interval)
        started = time.monotonic()
        requested_start, requested_end = requested_window_for_interval(
            interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        )
        trace = self._initial_trace(symbol, interval, source_interval, limit, requested_start, requested_end)
        self._record_initial_store_hits(trace, payload)
        trace["renderable"] = self._is_renderable(payload, interval, source_interval)

        if not self._needs_fill(payload, interval, source_interval, limit):
            trace["status"] = "not_needed"
            trace["durationMs"] = elapsed_ms(started)
            payload["fill"] = trace
            return payload

        fill_ranges = self._fill_ranges(payload, requested_start, requested_end)
        trace["missingRanges"] = fill_ranges
        trace["backgroundFill"] = self._enqueue_background_fill(
            symbol=symbol,
            interval=interval,
            source_interval=source_interval,
            limit=limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
            requested_start=requested_start,
            requested_end=requested_end,
            fill_ranges=fill_ranges,
        )
        trace["status"] = "partial" if payload.get("candles") else "empty"
        trace["durationMs"] = elapsed_ms(started)
        payload["fill"] = trace
        return payload

    def _initial_trace(self, symbol: str, interval: str, source_interval: str, limit: int, start: str, end: str) -> dict[str, Any]:
        return {
            "status": "not_needed",
            "symbol": symbol,
            "interval": interval,
            "sourceInterval": source_interval,
            "requestedLimit": limit,
            "requestedRange": {"start": start, "end": end},
            "sources": {source: {"checked": False, "hit": False, "rowCount": 0, "durationMs": 0, "error": None} for source in FILL_SOURCES},
            "missingRanges": [],
            "gapRanges": [],
            "renderable": False,
            "minimumReturnedCount": minimum_renderable_returned_bars(interval),
            "minimumRenderableSourceBars": minimum_renderable_source_bars(source_interval),
            "backgroundFill": {"queued": False, "state": "not_needed", "requestId": None, "reason": None},
            "durationMs": 0,
        }

    def _record_initial_store_hits(self, trace: dict[str, Any], payload: dict[str, Any]) -> None:
        source_trace = payload.pop("_sourceTrace", None)
        if isinstance(source_trace, dict):
            for name in ("redis", "clickhouse"):
                values = source_trace.get(name)
                if isinstance(values, dict):
                    trace["sources"][name].update({
                        "checked": bool(values.get("checked")),
                        "hit": bool(values.get("hit")),
                        "rowCount": int(values.get("rowCount") or 0),
                    })
            return

        row_count = len(payload.get("candles") or [])
        range_query = bool(payload.get("request", {}).get("before") or payload.get("request", {}).get("from") or payload.get("request", {}).get("to"))
        if not range_query:
            trace["sources"]["redis"].update({"checked": True, "hit": row_count > 0, "rowCount": row_count})
        trace["sources"]["clickhouse"].update({"checked": True, "hit": row_count > 0, "rowCount": row_count})

    def _needs_fill(self, payload: dict[str, Any], interval: str, source_interval: str, limit: int) -> bool:
        candles = payload.get("candles") or []
        if payload.get("missingRanges"):
            return True
        if not self._is_renderable(payload, interval, source_interval):
            return True
        return len(candles) < limit

    def _is_renderable(self, payload: dict[str, Any], interval: str, source_interval: str) -> bool:
        returned = int(payload.get("returnedCount") or len(payload.get("candles") or []))
        stored = int(payload.get("storedCandleCount") or returned)
        return bool(renderability_payload(interval, source_interval, payload.get("candles") or [], returned, stored).get("renderable"))

    def _fill_ranges(self, payload: dict[str, Any], requested_start: str, requested_end: str) -> list[dict[str, Any]]:
        ranges = payload.get("missingRanges") or []
        valid = [
            {"start": item["start"], "end": item["end"], "missingCount": item.get("missingCount")}
            for item in ranges
            if item.get("start") and item.get("end")
        ]
        if valid:
            return valid
        return [{"start": requested_start, "end": requested_end, "missingCount": None}]

    def _enqueue_background_fill(
        self,
        *,
        symbol: str,
        interval: str,
        source_interval: str,
        limit: int,
        before: str | None,
        from_time: str | None,
        to_time: str | None,
        requested_start: str,
        requested_end: str,
        fill_ranges: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.background_enabled:
            return {"queued": False, "state": "disabled", "requestId": None, "reason": "background fill disabled"}
        request_id = background_request_id(symbol, source_interval, fill_ranges)
        with _BACKGROUND_LOCK:
            if request_id in _BACKGROUND_ACTIVE:
                return {"queued": True, "state": "already_queued", "requestId": request_id, "reason": "matching fill already running"}
            _BACKGROUND_ACTIVE.add(request_id)
        try:
            executor = self.background_executor or background_executor()
            executor.submit(
                self._run_background_fill,
                request_id,
                symbol,
                interval,
                source_interval,
                limit,
                before,
                from_time,
                to_time,
                requested_start,
                requested_end,
                fill_ranges,
            )
        except Exception as exc:
            with _BACKGROUND_LOCK:
                _BACKGROUND_ACTIVE.discard(request_id)
            return {"queued": False, "state": "failed", "requestId": request_id, "reason": str(exc)}
        return {"queued": True, "state": "queued", "requestId": request_id, "reason": "range repair queued"}

    def _run_background_fill(
        self,
        request_id: str,
        symbol: str,
        interval: str,
        source_interval: str,
        limit: int,
        before: str | None,
        from_time: str | None,
        to_time: str | None,
        requested_start: str,
        requested_end: str,
        fill_ranges: list[dict[str, Any]],
    ) -> None:
        started = time.monotonic()
        trace = self._initial_trace(symbol, interval, source_interval, limit, requested_start, requested_end)
        trace["missingRanges"] = fill_ranges
        status = "failed"
        try:
            s3_filled = self._fill_from_s3(symbol, source_interval, fill_ranges, trace, started)
            if s3_filled:
                status = "filled"
                return
            if self._deadline_exceeded(started):
                status = "timeout"
                return
            alpaca_filled = self._fill_from_alpaca(symbol, source_interval, fill_ranges, trace, started)
            if alpaca_filled:
                status = "filled"
            elif self._deadline_exceeded(started):
                status = "timeout"
            else:
                status = "empty" if trace["sources"]["alpaca"].get("checked") and not trace["sources"]["alpaca"].get("error") else "failed"
        except Exception:
            logger.exception("On-demand chart background fill crashed for %s %s.", symbol, source_interval)
            status = "failed"
        finally:
            trace["status"] = status
            trace["durationMs"] = elapsed_ms(started)
            logger.info(
                "On-demand chart background fill finished.",
                extra={
                    "request_id": request_id,
                    "symbol": symbol,
                    "interval": interval,
                    "source_interval": source_interval,
                    "status": status,
                    "duration_ms": trace["durationMs"],
                    "sources": trace["sources"],
                },
            )
            with _BACKGROUND_LOCK:
                _BACKGROUND_ACTIVE.discard(request_id)

    def _fill_from_s3(self, symbol: str, interval: str, ranges: list[dict[str, Any]], trace: dict[str, Any], started: float) -> bool:
        source = trace["sources"]["s3"]
        source_started = time.monotonic()
        source["checked"] = True
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            source["error"] = "S3_BUCKET is not configured."
            source["durationMs"] = elapsed_ms(source_started)
            return False
        try:
            s3 = self._s3_client()
            final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
            keys: list[str] = []
            for fill_range in ranges:
                if self._deadline_exceeded(started):
                    break
                keys.extend(find_processed_candle_objects(
                    s3,
                    bucket,
                    final_prefix,
                    symbol,
                    interval,
                    fill_range["start"],
                    fill_range["end"],
                ))
            keys = unique_ordered(keys)
            source["hit"] = bool(keys)
            source["rowCount"] = len(keys)
            if not keys:
                return False
            result = materialize_s3_processed_objects(self._clickhouse_client(), s3, bucket, keys, source_name="on-demand-fill-s3")
            source["rowCount"] = int(result.get("rowCount") or 0)
            return source["rowCount"] > 0 or bool(keys)
        except Exception as exc:
            source["error"] = str(exc)
            return False
        finally:
            source["durationMs"] = elapsed_ms(source_started)

    def _fill_from_alpaca(self, symbol: str, interval: str, ranges: list[dict[str, Any]], trace: dict[str, Any], started: float) -> bool:
        source = trace["sources"]["alpaca"]
        source_started = time.monotonic()
        source["checked"] = True
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            source["error"] = "S3_BUCKET is required before Alpaca historical rows can be canonicalized."
            source["durationMs"] = elapsed_ms(source_started)
            return False
        if interval not in {"1m", "1D"}:
            source["error"] = f"On-demand Alpaca fill supports source intervals only: {interval}."
            source["durationMs"] = elapsed_ms(source_started)
            return False
        timeframe = "1Day" if interval == "1D" else "1Min"
        feed = historical_feed_for_symbol(symbol, os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip")))
        try:
            raw_rows = []
            for fill_range in ranges:
                if self._deadline_exceeded(started):
                    break
                raw_rows.extend(fetch_alpaca_bars(symbol, fill_range["start"], fill_range["end"], feed, timeframe))
            source["rowCount"] = len(raw_rows)
            source["hit"] = bool(raw_rows)
            if not raw_rows:
                return False
            s3 = self._s3_client()
            final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
            manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
            output_format = os.getenv("S3_PROCESSED_FORMAT", "parquet").lower()
            processed_source = repair_daily_bar_outliers(symbol, raw_rows, feed) if interval == "1D" else raw_rows
            processed = raw_bars_to_processed_candles(symbol, processed_source, feed=feed, interval=interval)
            if not processed:
                return False
            first_event_time = parse_time(processed[0]["timestamp"])
            partition_key = (
                f"{final_prefix}/candles/feed={feed or 'unknown'}/interval={interval}/symbol={symbol}"
                f"/year={first_event_time:%Y}/month={first_event_time:%m}/day={first_event_time:%d}"
                f"/source=on-demand-fill"
            )
            processed_key = flush_buffer(
                s3,
                bucket,
                partition_key,
                processed,
                output_format,
                manifest_prefix=manifest_prefix,
                manifest_layout=os.getenv("S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT", "compact"),
            )
            result = materialize_s3_processed_objects(self._clickhouse_client(), s3, bucket, [processed_key], source_name="on-demand-fill-alpaca")
            source["rowCount"] = int(result.get("rowCount") or len(processed))
            return True
        except BackfillUnavailable as exc:
            source["error"] = str(exc)
            return False
        except Exception as exc:
            source["error"] = str(exc)
            return False
        finally:
            source["durationMs"] = elapsed_ms(source_started)

    def _reload_payload(
        self,
        symbol: str,
        interval: str,
        limit: int,
        before: str | None,
        from_time: str | None,
        to_time: str | None,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.provider:
            return fallback
        try:
            return self.provider.candle_snapshot(symbol, interval, limit, before=before, from_time=from_time, to_time=to_time)
        except Exception:
            return fallback

    def _record_refreshed_result(self, trace: dict[str, Any], payload: dict[str, Any]) -> None:
        source_trace = payload.pop("_sourceTrace", None)
        if isinstance(source_trace, dict):
            for name in ("redis", "clickhouse"):
                values = source_trace.get(name)
                if isinstance(values, dict):
                    trace["sources"][name].update({
                        "checked": bool(values.get("checked")),
                        "hit": bool(values.get("hit")),
                        "rowCount": int(values.get("rowCount") or 0),
                    })
            return
        candles = payload.get("candles") or []
        trace["sources"]["clickhouse"].update({"checked": True, "hit": bool(candles), "rowCount": len(candles)})

    def _deadline_exceeded(self, started: float) -> bool:
        return time.monotonic() - started >= self.timeout_seconds

    def _s3_client(self):
        if self.s3 is not None:
            return self.s3
        from alfaka.common.s3_client import create_s3_client

        self.s3 = create_s3_client()
        return self.s3

    def _clickhouse_client(self):
        if self.clickhouse_client is not None:
            return self.clickhouse_client
        self.clickhouse_client = ClickHouseHttpClient(
            url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            user=os.getenv("CLICKHOUSE_USER", "alfaka"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )
        if should_ensure_schema_on_start() and hasattr(self.clickhouse_client, "ensure_market_data_schema"):
            self.clickhouse_client.ensure_market_data_schema()
        return self.clickhouse_client


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def background_request_id(symbol: str, interval: str, ranges: list[dict[str, Any]]) -> str:
    body = json.dumps({"symbol": symbol, "interval": interval, "ranges": ranges}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:20]


def background_executor() -> ThreadPoolExecutor:
    global _BACKGROUND_EXECUTOR
    if _BACKGROUND_EXECUTOR is None:
        workers = max(1, int(os.getenv("ON_DEMAND_FILL_BACKGROUND_WORKERS", "2")))
        _BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chart-fill")
    return _BACKGROUND_EXECUTOR


def get_on_demand_fill_service(provider=None) -> OnDemandFillService:
    return OnDemandFillService(provider=provider)
