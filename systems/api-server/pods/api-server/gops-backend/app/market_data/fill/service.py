from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any

from alfaka.alpaca.feed_profiles import MARKET_TIMEZONE, market_session_for_datetime
from alfaka.backfill.runner import (
    BackfillUnavailable,
    canonical_historical_candles,
    fetch_alpaca_bars,
    find_processed_candle_objects,
    historical_feed_for_symbol,
    persist_historical_source_candles,
    repair_daily_bar_outliers,
)
from alfaka.serving.dto import candle_to_gops, cursor_for
from alfaka.serving.intervals import (
    INTRADAY_DERIVED_INTERVALS,
    alpaca_timeframe_for_interval,
    historical_source_interval_for,
    interval_seconds,
    minimum_renderable_returned_bars,
    minimum_renderable_source_bars,
    normalize_chart_interval,
    source_interval_for,
)
from alfaka.common.symbols import is_crypto_symbol
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.serving.provider import merge_candles, requested_source_bar_target, requested_window_for_interval
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient, should_ensure_schema_on_start
from alfaka.storage.processed_s3_sink import flush_buffer
from alfaka.storage.s3_manifest import DEFAULT_MANIFEST_PREFIX
from alfaka.storage.s3_materializer import materialize_s3_processed_objects
from app.market_data.backfill.service import renderability_payload
from app.market_data.redis_atomic import compare_and_delete, compare_and_set_ex


FILL_TIMEOUT_SECONDS = 30.0
BACKGROUND_FILL_TIMEOUT_SECONDS = 60.0
FILL_SOURCES = ("redis", "clickhouse", "s3", "alpaca")
INTRADAY_HISTORICAL_ROUTE_INTERVALS = {"1m", "5m", "10m", "1h", "4h"}
US_EQUITY_SESSION_BOUNDARY_TIMES = (
    datetime_time(4, 0),
    datetime_time(9, 30),
    datetime_time(16, 0),
    datetime_time(20, 0),
)
logger = logging.getLogger(__name__)

_BACKGROUND_EXECUTOR: ThreadPoolExecutor | None = None
_BACKGROUND_ACTIVE: set[str] = set()
_BACKGROUND_LOCK = threading.Lock()


class DistributedFillSingleflight:
    def __init__(self, redis_client, *, lock_ttl_seconds: int | None = None, terminal_ttl_seconds: int | None = None):
        self.redis = redis_client
        self.enabled = os.getenv("ON_DEMAND_FILL_DISTRIBUTED_SINGLEFLIGHT_ENABLED", "true").lower() not in {
            "0", "false", "off", "no"
        }
        self.lock_ttl_seconds = lock_ttl_seconds or max(
            1, int(os.getenv("ON_DEMAND_FILL_SINGLEFLIGHT_LOCK_TTL_SECONDS", "120"))
        )
        self.terminal_ttl_seconds = terminal_ttl_seconds or max(
            1, int(os.getenv("ON_DEMAND_FILL_SINGLEFLIGHT_TERMINAL_TTL_SECONDS", "300"))
        )

    def acquire(self, request_id: str) -> tuple[bool, str | None, str]:
        if not self.enabled or self.redis is None:
            return True, None, "local_only"
        owner_token = uuid.uuid4().hex
        value = self._value("running", owner_token)
        try:
            acquired = self.redis.set(
                self._key(request_id),
                value,
                nx=True,
                ex=self.lock_ttl_seconds,
            )
        except TypeError:
            return True, None, "local_only"
        except Exception:
            return True, None, "local_only"
        if acquired:
            return True, owner_token, "queued"
        return False, None, self._existing_state(request_id)

    def complete(self, request_id: str, owner_token: str | None, status: str) -> bool:
        if not owner_token or self.redis is None:
            return False
        return compare_and_set_ex(
            self.redis,
            self._key(request_id),
            self._value("running", owner_token),
            self._value("terminal", owner_token, status=status),
            self.terminal_ttl_seconds,
        )

    def release(self, request_id: str, owner_token: str | None) -> bool:
        if not owner_token or self.redis is None:
            return False
        return compare_and_delete(
            self.redis,
            self._key(request_id),
            self._value("running", owner_token),
        )

    def _existing_state(self, request_id: str) -> str:
        try:
            payload = parse_singleflight_value(self.redis.get(self._key(request_id)))
        except Exception:
            payload = {}
        return "terminal" if payload.get("state") == "terminal" else "already_queued"

    @staticmethod
    def _value(state: str, owner_token: str, *, status: str | None = None) -> str:
        return json.dumps(
            {"state": state, "owner": owner_token, "status": status},
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _key(request_id: str) -> str:
        prefix = os.getenv("REDIS_KEY_PREFIX", "gops:market:on-demand:v1").strip().strip(":")
        namespace = f"{prefix}:" if prefix else ""
        return f"{namespace}fill:singleflight:v1:{request_id}"


def parse_singleflight_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
        self.foreground_enabled = os.getenv("ON_DEMAND_FILL_FOREGROUND_ALPACA_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        self.foreground_max_bars = max(1, int(os.getenv("ON_DEMAND_FILL_FOREGROUND_MAX_BARS", os.getenv("HISTORICAL_LIMIT", "10000"))))
        self.foreground_auto_intervals = parse_interval_set(
            os.getenv("ON_DEMAND_FILL_FOREGROUND_AUTO_INTERVALS", "1D,1W,1M")
        )
        self.foreground_auto_max_bars = max(1, int(os.getenv("ON_DEMAND_FILL_FOREGROUND_AUTO_MAX_BARS", "500")))
        self.background_executor = background_executor
        redis_provider = getattr(provider, "redis_provider", None)
        self.singleflight = DistributedFillSingleflight(getattr(redis_provider, "redis", None))

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
        source_interval = normalize_chart_interval(payload.get("sourceInterval") or source_interval_for(interval))
        started = time.monotonic()
        requested_start, requested_end = requested_window_for_interval(
            interval,
            limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
        )
        range_query = bool(before or from_time or to_time)
        requested_shortfalls = (
            requested_window_shortfall_ranges(payload, requested_start, requested_end, interval)
            if range_query
            else []
        )
        trace = self._initial_trace(symbol, interval, source_interval, limit, requested_start, requested_end)
        self._record_initial_store_hits(trace, payload)
        renderability = self._renderability(payload, interval, source_interval)
        trace["renderable"] = bool(renderability.get("renderable"))
        trace["gapRanges"] = renderability.get("gapRanges") or []

        if not self._needs_fill(payload, interval, source_interval, limit, requested_shortfalls):
            trace["status"] = "not_needed"
            trace["durationMs"] = elapsed_ms(started)
            payload["fill"] = trace
            return payload

        fill_ranges = self._fill_ranges(
            payload,
            requested_start,
            requested_end,
            interval,
            source_interval,
            requested_shortfalls,
        )
        trace["missingRanges"] = fill_ranges
        trace["feedRoutes"] = historical_fill_routes(
            symbol,
            interval,
            fill_ranges,
            os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip")),
        )
        foreground_filled = self._fill_foreground_from_alpaca(
            symbol=symbol,
            interval=interval,
            limit=limit,
            before=before,
            from_time=from_time,
            to_time=to_time,
            requested_start=requested_start,
            requested_end=requested_end,
            ranges=fill_ranges,
            payload=payload,
            trace=trace,
            started=started,
        )
        if foreground_filled:
            source_interval = normalize_chart_interval(payload.get("sourceInterval") or interval)
            trace["sourceInterval"] = source_interval
            trace["minimumRenderableSourceBars"] = minimum_renderable_source_bars(source_interval)
            post_renderability = self._renderability(payload, interval, source_interval)
            trace["renderable"] = bool(post_renderability.get("renderable"))
            trace["gapRanges"] = post_renderability.get("gapRanges") or []
            trace["missingRanges"] = trace["gapRanges"]
            trace["backgroundFill"] = self._enqueue_background_fill(
                symbol=symbol,
                interval=interval,
                source_interval=interval,
                limit=limit,
                before=before,
                from_time=from_time,
                to_time=to_time,
                requested_start=requested_start,
                requested_end=requested_end,
                fill_ranges=fill_ranges,
            )
            trace["status"] = "filled" if trace["renderable"] else "partial"
            trace["durationMs"] = elapsed_ms(started)
            payload["fill"] = trace
            return payload
        trace["backgroundFill"] = self._enqueue_background_fill(
            symbol=symbol,
            interval=interval,
            source_interval=interval,
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

    def _fill_foreground_from_alpaca(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        before: str | None,
        from_time: str | None,
        to_time: str | None,
        requested_start: str,
        requested_end: str,
        ranges: list[dict[str, Any]],
        payload: dict[str, Any],
        trace: dict[str, Any],
        started: float,
    ) -> bool:
        source = trace["sources"]["alpaca"]
        trace["foregroundFill"] = {"attempted": False, "source": "alpaca-rest-direct", "rowCount": 0, "state": "not_needed", "reason": None}
        routes = trace.get("feedRoutes") or historical_fill_routes(
            symbol,
            interval,
            ranges,
            os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip")),
        )
        trace["feedRoutes"] = routes
        fetch_routes = [route for route in routes if route.get("state") == "fetchable"]
        provider_interval = historical_fill_source_interval(symbol, interval, fetch_routes)
        estimated_bars = estimated_bar_count(provider_interval, fetch_ranges_from_routes(fetch_routes))
        foreground_auto_enabled = interval in self.foreground_auto_intervals and estimated_bars <= self.foreground_auto_max_bars
        if not fetch_routes:
            trace["foregroundFill"].update({
                "state": "skipped",
                "reason": "requested range has no historical REST fillable session",
            })
            return False
        if not self.foreground_enabled and not foreground_auto_enabled:
            trace["foregroundFill"].update({"state": "disabled", "reason": "foreground Alpaca fill disabled"})
            return False
        foreground_cap = self.foreground_max_bars
        if foreground_auto_enabled:
            foreground_cap = max(foreground_cap, self.foreground_auto_max_bars)
        if estimated_bars > foreground_cap:
            trace["foregroundFill"].update({
                "state": "skipped",
                "reason": f"estimated bar count {estimated_bars} exceeds foreground cap {foreground_cap}",
            })
            return False
        if self._deadline_exceeded(started):
            trace["foregroundFill"].update({"state": "timeout", "reason": "fill deadline exceeded before foreground request"})
            return False
        source_started = time.monotonic()
        source["checked"] = True
        trace["foregroundFill"]["attempted"] = True
        try:
            timeframe = alpaca_timeframe_for_interval(provider_interval)
            raw_by_feed: list[tuple[str, list[dict[str, Any]]]] = []
            for route in fetch_routes:
                if self._deadline_exceeded(started):
                    break
                route_rows = fetch_alpaca_bars(symbol, route["start"], route["end"], route["feed"], timeframe)
                route["rowCount"] = len(route_rows)
                raw_by_feed.append((route["feed"], route_rows))
            source["rowCount"] = sum(len(rows) for _, rows in raw_by_feed)
            source["hit"] = source["rowCount"] > 0
            trace["foregroundFill"]["rowCount"] = source["rowCount"]
            if source["rowCount"] <= 0:
                trace["foregroundFill"].update({"state": "empty", "reason": "Alpaca returned no bars for requested range"})
                return False
            processed = []
            first_feed = fetch_routes[0]["feed"]
            for feed, raw_rows in raw_by_feed:
                if not raw_rows:
                    continue
                processed_source = repair_daily_bar_outliers(symbol, raw_rows, feed) if interval == "1D" else raw_rows
                _, target_candles = canonical_historical_candles(
                    symbol,
                    processed_source,
                    feed=feed,
                    interval=interval,
                    source_interval=provider_interval,
                    completed_through=parse_time(requested_end),
                )
                processed.extend(target_candles)
            direct_candles = [
                {
                    **candle_to_gops(candle),
                    "symbol": symbol,
                    "timeframe": interval,
                    "sourceInterval": interval,
                }
                for candle in processed
            ]
            direct_candles = [
                candle for candle in direct_candles
                if candle_in_window(candle, before=before, from_time=from_time or requested_start, to_time=to_time or requested_end)
            ]
            if not direct_candles:
                trace["foregroundFill"].update({"state": "empty", "reason": "Alpaca bars fell outside requested chart window"})
                return False
            merged = merge_candles(direct_candles, payload.get("candles") or [])
            candles = attach_moving_averages(merged, overwrite=True)[-limit:]
            payload.update({
                "source": "alpaca",
                "feed": first_feed,
                "sourceInterval": interval,
                "snapshotCursor": cursor_for(symbol, interval, candles[-1]) if candles else None,
                "candles": candles,
                "returnedCount": len(candles),
                "storedCandleCount": len(candles),
                "targetStoredCount": requested_source_bar_target(interval, limit, source_interval=interval),
                "availableFrom": candles[0].get("timestamp") if candles else None,
                "availableTo": candles[-1].get("timestamp") if candles else None,
                "oldestTimestamp": candles[0].get("timestamp") if candles else None,
                "newestTimestamp": candles[-1].get("timestamp") if candles else None,
                "hasMoreBefore": len(candles) >= limit,
                "hasMoreAfter": False,
                "missingRanges": [],
                "_foregroundAlpacaFilled": True,
            })
            trace["foregroundFill"].update({"state": "filled", "reason": None})
            return True
        except BackfillUnavailable as exc:
            source["error"] = str(exc)
            trace["foregroundFill"].update({"state": "failed", "reason": str(exc)})
            return False
        except Exception as exc:
            source["error"] = str(exc)
            trace["foregroundFill"].update({"state": "failed", "reason": str(exc)})
            return False
        finally:
            source["durationMs"] = elapsed_ms(source_started)

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
            "feedRoutes": [],
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

    def _needs_fill(
        self,
        payload: dict[str, Any],
        interval: str,
        source_interval: str,
        limit: int,
        requested_shortfalls: list[dict[str, Any]],
    ) -> bool:
        candles = payload.get("candles") or []
        if payload.get("missingRanges"):
            return True
        if opportunistic_intraday_gap_ranges(interval, candles):
            return True
        if requested_shortfalls:
            return True
        if self._requested_tiny_window_is_satisfied(payload, interval, limit):
            return False
        if not self._is_renderable(payload, interval, source_interval):
            return True
        return len(candles) < limit

    def _requested_tiny_window_is_satisfied(self, payload: dict[str, Any], interval: str, limit: int) -> bool:
        if limit >= minimum_renderable_returned_bars(interval):
            return False
        candles = payload.get("candles") or []
        returned = int(payload.get("returnedCount") or len(candles))
        return returned >= limit and len(candles) >= limit

    def _is_renderable(self, payload: dict[str, Any], interval: str, source_interval: str) -> bool:
        return bool(self._renderability(payload, interval, source_interval).get("renderable"))

    def _renderability(self, payload: dict[str, Any], interval: str, source_interval: str) -> dict[str, Any]:
        returned = int(payload.get("returnedCount") or len(payload.get("candles") or []))
        stored = int(payload.get("storedCandleCount") or returned)
        return renderability_payload(interval, source_interval, payload.get("candles") or [], returned, stored)

    def _fill_ranges(
        self,
        payload: dict[str, Any],
        requested_start: str,
        requested_end: str,
        interval: str,
        source_interval: str,
        requested_shortfalls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ranges = list(payload.get("missingRanges") or [])
        ranges.extend(payload.get("gapRanges") or [])
        coverage = payload.get("coverage") or {}
        ranges.extend(coverage.get("missingRanges") or [])
        ranges.extend(coverage.get("gapRanges") or [])
        ranges.extend(self._renderability(payload, interval, source_interval).get("gapRanges") or [])
        ranges.extend(opportunistic_intraday_gap_ranges(interval, payload.get("candles") or []))
        ranges.extend(requested_shortfalls)
        valid = unique_ranges([
            {"start": item["start"], "end": item["end"], "missingCount": item.get("missingCount")}
            for item in ranges
            if item.get("start") and item.get("end") and item.get("start") < item.get("end")
        ])
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
            acquired, owner_token, distributed_state = self.singleflight.acquire(request_id)
            if not acquired:
                return {
                    "queued": True,
                    "state": distributed_state,
                    "requestId": request_id,
                    "reason": "matching fill is owned by another API replica",
                }
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
                owner_token,
            )
        except Exception as exc:
            with _BACKGROUND_LOCK:
                _BACKGROUND_ACTIVE.discard(request_id)
            self.singleflight.release(request_id, owner_token)
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
        owner_token: str | None = None,
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
                "On-demand chart background fill finished: request_id=%s symbol=%s interval=%s source_interval=%s status=%s duration_ms=%s sources=%s feed_routes=%s",
                request_id,
                symbol,
                interval,
                source_interval,
                status,
                trace["durationMs"],
                json.dumps(trace["sources"], sort_keys=True),
                json.dumps(trace.get("feedRoutes") or [], sort_keys=True),
            )
            with _BACKGROUND_LOCK:
                _BACKGROUND_ACTIVE.discard(request_id)
            self.singleflight.complete(request_id, owner_token, status)

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
            if not keys:
                return False
            result = materialize_s3_processed_objects(
                self._clickhouse_client(),
                s3,
                bucket,
                keys,
                source_name="on-demand-fill-s3",
                selection={"symbol": symbol, "interval": interval, "ranges": ranges},
            )
            source["rowCount"] = int(result.get("matchedRowCount") or 0)
            source["hit"] = source["rowCount"] > 0
            return source["hit"]
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
        routes = historical_fill_routes(symbol, interval, ranges, os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip")))
        trace["feedRoutes"] = routes
        fetch_routes = [route for route in routes if route.get("state") == "fetchable"]
        if not fetch_routes:
            source["durationMs"] = elapsed_ms(source_started)
            return False
        provider_interval = historical_fill_source_interval(symbol, interval, fetch_routes)
        timeframe = alpaca_timeframe_for_interval(provider_interval)
        try:
            raw_by_feed: list[tuple[str, list[dict[str, Any]]]] = []
            for route in fetch_routes:
                if self._deadline_exceeded(started):
                    break
                route_rows = fetch_alpaca_bars(symbol, route["start"], route["end"], route["feed"], timeframe)
                route["rowCount"] = len(route_rows)
                raw_by_feed.append((route["feed"], route_rows))
            source["rowCount"] = sum(len(rows) for _, rows in raw_by_feed)
            source["hit"] = source["rowCount"] > 0
            if source["rowCount"] <= 0:
                return False
            s3 = self._s3_client()
            final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
            manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
            output_format = os.getenv("S3_PROCESSED_FORMAT", "parquet").lower()
            processed = []
            source_candles = []
            first_feed = fetch_routes[0]["feed"]
            for feed, raw_rows in raw_by_feed:
                if not raw_rows:
                    continue
                processed_source = repair_daily_bar_outliers(symbol, raw_rows, feed) if interval == "1D" else raw_rows
                canonical_source, target_candles = canonical_historical_candles(
                    symbol,
                    processed_source,
                    feed=feed,
                    interval=interval,
                    source_interval=provider_interval,
                    completed_through=max(parse_time(route["end"]) for route in fetch_routes),
                )
                source_candles.extend(canonical_source)
                processed.extend(target_candles)
            if not processed:
                return False
            persist_historical_source_candles(
                self._clickhouse_client(),
                source_candles,
                source_interval=provider_interval,
                interval=interval,
            )
            first_event_time = parse_time(processed[0]["timestamp"])
            partition_key = (
                f"{final_prefix}/candles/feed={first_feed or 'unknown'}/interval={interval}/symbol={symbol}"
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


def safe_parse_time(value: Any) -> datetime | None:
    try:
        return parse_time(value)
    except (TypeError, ValueError):
        return None


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def unique_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in sorted(ranges, key=lambda value: (value["start"], value["end"])):
        key = (item["start"], item["end"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def parse_interval_set(value: str | None) -> set[str]:
    intervals: set[str] = set()
    for item in str(value or "").split(","):
        trimmed = item.strip()
        if not trimmed:
            continue
        try:
            intervals.add(normalize_chart_interval(trimmed))
        except ValueError:
            continue
    return intervals


def background_request_id(symbol: str, interval: str, ranges: list[dict[str, Any]]) -> str:
    body = json.dumps({"symbol": symbol, "interval": interval, "ranges": ranges}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:20]


def estimated_bar_count(interval: str, ranges: list[dict[str, Any]]) -> int:
    seconds = max(1, interval_seconds(interval))
    count = 0
    for fill_range in ranges:
        start = parse_time(fill_range.get("start"))
        end = parse_time(fill_range.get("end"))
        if not start or not end or end <= start:
            continue
        count += int((end - start).total_seconds() // seconds) + 1
    return count


def historical_fill_routes(symbol: str, interval: str, ranges: list[dict[str, Any]], default_feed: str) -> list[dict[str, Any]]:
    interval = normalize_chart_interval(interval)
    feed = historical_feed_for_symbol(symbol, default_feed)
    if is_crypto_symbol(symbol) or interval not in INTRADAY_HISTORICAL_ROUTE_INTERVALS:
        return [
            {
                "start": item["start"],
                "end": item["end"],
                "feed": feed,
                "session": "all",
                "state": "fetchable",
                "reason": None,
            }
            for item in ranges
            if item.get("start") and item.get("end")
        ]

    routes: list[dict[str, Any]] = []
    for item in ranges:
        start_raw = item.get("start")
        end_raw = item.get("end")
        if not start_raw or not end_raw:
            continue
        start = parse_time(start_raw)
        end = parse_time(end_raw)
        if end <= start:
            continue
        cursor = start
        while cursor < end:
            segment_end = min(next_us_equity_session_boundary(cursor), end)
            if segment_end <= cursor:
                break
            session = market_session_for_datetime(cursor)
            if session == "regular":
                route_feed = feed
                state = "fetchable"
                reason = None
            elif session in {"pre", "after"}:
                route_feed = feed
                state = "fetchable"
                reason = None
            elif session == "overnight":
                route_feed = "boats"
                state = "fetchable"
                reason = None
            else:
                route_feed = None
                state = "skipped"
                reason = "market is closed for this range"
            routes.append({
                "start": iso_utc(cursor),
                "end": iso_utc(segment_end),
                "feed": route_feed,
                "session": session,
                "state": state,
                "reason": reason,
            })
            cursor = segment_end
    return merge_adjacent_routes(routes)


def historical_fill_source_interval(symbol: str, interval: str, routes: list[dict[str, Any]]) -> str:
    """Use 1m source bars whenever a derived stock repair crosses an extended session."""
    interval = normalize_chart_interval(interval)
    if is_crypto_symbol(symbol):
        return interval
    has_extended_session = any(
        route.get("state") == "fetchable" and route.get("session") in {"pre", "after", "overnight"}
        for route in routes
    )
    if interval in INTRADAY_DERIVED_INTERVALS and has_extended_session:
        return "1m"
    return historical_source_interval_for(interval)


def opportunistic_intraday_gap_ranges(interval: str, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    interval = normalize_chart_interval(interval)
    if interval not in INTRADAY_HISTORICAL_ROUTE_INTERVALS:
        return []
    timestamps = sorted(
        timestamp
        for timestamp in (safe_parse_time(candle.get("timestamp")) for candle in candles if candle.get("timestamp"))
        if timestamp is not None
    )
    if len(timestamps) < 2:
        return []
    allowed_gap = interval_seconds(interval) * 3
    bucket_delta = timedelta(seconds=interval_seconds(interval))
    ranges = []
    for previous, current in zip(timestamps, timestamps[1:]):
        gap_seconds = (current - previous).total_seconds()
        if gap_seconds <= allowed_gap:
            continue
        previous_session = market_session_for_datetime(previous)
        current_session = market_session_for_datetime(current)
        if previous_session != current_session or previous_session not in {"pre", "regular", "after", "overnight"}:
            continue
        if market_session_identity(previous, previous_session) != market_session_identity(current, current_session):
            continue
        missing_count = max(1, int(gap_seconds // interval_seconds(interval)) - 1)
        ranges.append({
            "start": iso_utc(previous + bucket_delta),
            "end": iso_utc(current),
            "missingCount": missing_count,
        })
    return ranges


def market_session_identity(value: datetime, session: str) -> tuple[str, str]:
    local = value.astimezone(MARKET_TIMEZONE)
    trading_date = local.date()
    if session == "overnight" and local.time() >= datetime_time(20, 0):
        trading_date += timedelta(days=1)
    return session, trading_date.isoformat()


def requested_window_shortfall_ranges(
    payload: dict[str, Any],
    requested_start: str,
    requested_end: str,
    interval: str,
) -> list[dict[str, Any]]:
    candles = payload.get("candles") or []
    if not candles:
        return []
    start = safe_parse_time(requested_start)
    end = safe_parse_time(requested_end)
    available_from = safe_parse_time(payload.get("availableFrom") or candles[0].get("timestamp"))
    available_to = safe_parse_time(payload.get("availableTo") or candles[-1].get("timestamp"))
    if start is None or end is None or available_from is None or available_to is None:
        return []

    ranges = []
    if start < available_from:
        ranges.append({
            "start": iso_utc(start),
            "end": iso_utc(min(available_from, end)),
            "missingCount": None,
        })
    available_to_coverage_end = available_to + timedelta(seconds=interval_seconds(interval))
    if available_to_coverage_end < end:
        ranges.append({
            "start": iso_utc(max(available_to_coverage_end, start)),
            "end": iso_utc(end),
            "missingCount": None,
        })
    return [item for item in ranges if item["start"] < item["end"]]


def next_us_equity_session_boundary(value: datetime) -> datetime:
    local = value.astimezone(MARKET_TIMEZONE)
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        local_date = local.date() + timedelta(days=day_offset)
        for boundary_time in US_EQUITY_SESSION_BOUNDARY_TIMES:
            candidate = datetime.combine(local_date, boundary_time, tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
            if candidate > value:
                candidates.append(candidate)
    return min(candidates) if candidates else value + timedelta(days=1)


def merge_adjacent_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for route in routes:
        previous = merged[-1] if merged else None
        if (
            previous
            and previous.get("end") == route.get("start")
            and previous.get("feed") == route.get("feed")
            and previous.get("session") == route.get("session")
            and previous.get("state") == route.get("state")
            and previous.get("reason") == route.get("reason")
        ):
            previous["end"] = route["end"]
            continue
        merged.append(dict(route))
    return merged


def fetch_ranges_from_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"start": route["start"], "end": route["end"]} for route in routes if route.get("start") and route.get("end")]


def candle_in_window(candle: dict[str, Any], *, before: str | None, from_time: str | None, to_time: str | None) -> bool:
    timestamp = parse_time(candle.get("timestamp"))
    if not timestamp:
        return False
    before_time = parse_time(before) if before else None
    start_time = parse_time(from_time) if from_time else None
    end_time = parse_time(to_time) if to_time else None
    if before_time and timestamp >= before_time:
        return False
    if start_time and timestamp < start_time:
        return False
    if end_time and timestamp > end_time:
        return False
    return True


def background_executor() -> ThreadPoolExecutor:
    global _BACKGROUND_EXECUTOR
    if _BACKGROUND_EXECUTOR is None:
        workers = max(1, int(os.getenv("ON_DEMAND_FILL_BACKGROUND_WORKERS", "2")))
        _BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chart-fill")
    return _BACKGROUND_EXECUTOR


def get_on_demand_fill_service(provider=None) -> OnDemandFillService:
    return OnDemandFillService(provider=provider)
