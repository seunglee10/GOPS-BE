import json
import os
import re
import time
from datetime import timedelta, timezone

from alfaka.backfill.gapfill import TradingCalendar, detect_gapfill_ranges, parse_time
from alfaka.common.canonical import CANONICAL_VERSION, candle_metadata, historical_adjustment_from_env
from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.market_messages import source_event_id
from alfaka.common.symbols import alpaca_provider_symbol, is_crypto_symbol
from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.serving.intervals import (
    INTRADAY_DERIVED_INTERVALS,
    INTRADAY_INTERVAL_MINUTES,
    alpaca_timeframe_for_interval,
    historical_source_interval_for,
    normalize_chart_interval,
)
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.serving.session_buckets import (
    BUCKET_POLICY_SOURCE_NATIVE,
    aggregate_extended_session_candles,
    aggregate_regular_session_candles,
)
from alfaka.storage.clickhouse_loader import (
    ClickHouseHttpClient,
    candle_to_clickhouse_row,
    should_ensure_schema_on_start,
)
from alfaka.storage.processed_s3_sink import flush_buffer
from alfaka.storage.s3_manifest import (
    DEFAULT_MANIFEST_PREFIX,
    analysis_repair_processed_candle_keys,
    bounded_processed_candle_partition_keys,
    processed_candle_keys_from_manifest,
    require_canonical_processed_manifest,
)
from alfaka.storage.s3_materializer import (
    commit_prepared_s3_processed_objects,
    materialize_s3_processed_objects,
    prepare_s3_processed_objects,
)
from alfaka.streaming.transforms import normalize_bar


class BackfillUnavailable(RuntimeError):
    pass


class BackfillDeadlineExceeded(BackfillUnavailable):
    def __init__(self, message, *, metrics=None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


class BackfillRunner:
    def __init__(
        self,
        store=None,
        s3=None,
        clickhouse_client=None,
        coverage_provider=None,
        s3_operation_timeout_seconds=None,
    ):
        """backfill 실행에 필요한 S3, ClickHouse, coverage 조회 의존성을 준비합니다."""
        load_dotenv()
        if s3 is None:
            from alfaka.common.s3_client import create_s3_client

            s3 = create_s3_client(operation_timeout_seconds=s3_operation_timeout_seconds)
        self.store = store
        self.s3 = s3
        self.clickhouse_client = clickhouse_client or ClickHouseHttpClient(
            url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            user=os.getenv("CLICKHOUSE_USER", "alfaka"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )
        if should_ensure_schema_on_start() and hasattr(self.clickhouse_client, "ensure_market_data_schema"):
            self.clickhouse_client.ensure_market_data_schema()
        self.coverage_provider = coverage_provider or ClickHouseMarketDataProvider(
            url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            user=os.getenv("CLICKHOUSE_USER", "alfaka"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )

    def run(self, record):
        """backfill job 상태를 running/succeeded/failed로 갱신하며 실제 작업을 실행합니다."""
        current = record
        if self.store:
            current = self.store.update_status(current, "running")
        try:
            result = self._run(current)
        except BackfillUnavailable as exc:
            if self.store:
                return self.store.update_status(current, "unavailable", error=str(exc))
            raise
        except Exception as exc:
            if self.store:
                return self.store.update_status(current, "failed", error=str(exc))
            raise

        if self.store:
            return self.store.update_status(current, "succeeded", result=result)
        return {**current, "status": "succeeded", "result": result}

    def prepare_analysis_s3_repair(self, record):
        """Finish every fallible S3 read before the caller's hard deadline."""
        if self.store is not None:
            raise BackfillUnavailable("Analysis repair preparation must stay request-local.")
        return self.run({**record, "_analysisPrepareOnly": True})

    def commit_analysis_s3_repair(self, outcome):
        """Commit only a preparation that the request thread accepted in time."""
        if not isinstance(outcome, dict) or not isinstance(outcome.get("result"), dict):
            raise BackfillUnavailable("Invalid prepared analysis repair result.")
        result = dict(outcome["result"])
        prepared = result.pop("_preparedMaterialization", None)
        if not isinstance(prepared, dict):
            raise BackfillUnavailable("Analysis repair preparation is missing.")
        committed = commit_prepared_s3_processed_objects(
            self.clickhouse_client,
            prepared,
            source_name="chart-analysis-repair-s3-processed",
            write_object_audits=False,
        )
        result.update({
            "materializedRowCount": int(committed.get("matchedRowCount") or 0),
            "materialization": committed,
        })
        return {**outcome, "result": result}

    def _run(self, record):
        """단일 backfill 요청을 sourcePreference와 jobType에 맞춰 처리합니다."""
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            raise BackfillUnavailable("S3_BUCKET is required for backfill.")

        symbol = record["symbol"]
        interval = normalize_chart_interval(record["interval"])
        start = record["range"]["start"]
        end = record["range"]["end"]
        mode = record.get("mode") or os.getenv("BACKFILL_EXECUTION_MODE", "queue")
        job_type = (record.get("jobType") or "gapfill").strip().lower()
        force_refresh = bool(record.get("force"))
        source_preference = normalize_source_preference(record.get("sourcePreference", "coverage-first"))
        feed = historical_feed_for_symbol(symbol, os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip")))

        analysis_repair_ranges = record.get("analysisRepairRanges")
        if analysis_repair_ranges is not None:
            if source_preference != "s3-only" or interval != "1D":
                raise BackfillUnavailable("Analysis repair batching only supports sourcePreference=s3-only and interval=1D.")
            return self._run_analysis_s3_repair(
                bucket,
                symbol,
                interval,
                analysis_repair_ranges,
                deadline_monotonic=record.get("_deadlineMonotonic"),
                cancel_check=record.get("_cancelCheck"),
                lookup_metrics=record.get("_lookupMetrics"),
                prepare_only=bool(record.get("_analysisPrepareOnly")),
            )

        if job_type not in {"initial_load", "gapfill", "replay_repair", "correction_replay"}:
            raise BackfillUnavailable(f"Unsupported backfill job type: {job_type}.")
        if job_type in {"replay_repair", "correction_replay"}:
            return self._run_replay_job(bucket, symbol, interval, start, end, job_type, source_preference)

        coverage = None
        repair_ranges = [{"start": start, "end": end, "missingCount": None}]
        clickhouse_covered = False
        if source_preference == "coverage-first":
            coverage = self.coverage_provider.candle_coverage(symbol, interval)
            detected_ranges = self.detect_missing_ranges(symbol, interval, start, end, job_type)
            if detected_ranges is not None:
                if not detected_ranges:
                    return {
                        "jobType": job_type,
                        "sourcePreference": source_preference,
                        "source": "clickhouse",
                        "skipped": True,
                        "reason": "requested range has no missing expected buckets",
                        "coverage": coverage,
                    }
                repair_ranges = detected_ranges
            elif not force_refresh and range_is_covered_by_clickhouse(coverage, start, end):
                clickhouse_covered = True
                if job_type != "initial_load":
                    return {
                        "jobType": job_type,
                        "sourcePreference": source_preference,
                        "source": "clickhouse",
                        "skipped": True,
                        "reason": "requested range is already covered in ClickHouse",
                        "coverage": coverage,
                    }

        if (not force_refresh or source_preference == "s3-only") and source_preference in {"coverage-first", "s3-only"} and (job_type != "initial_load" or source_preference == "s3-only"):
            final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
            processed_keys = []
            for repair_range in repair_ranges:
                processed_keys.extend(find_processed_candle_objects(
                    self.s3,
                    bucket,
                    final_prefix,
                    symbol,
                    interval,
                    repair_range["start"],
                    repair_range["end"],
                ))
            processed_keys = unique_ordered(processed_keys)
            if processed_keys:
                materialized = materialize_s3_processed_objects(
                    self.clickhouse_client,
                    self.s3,
                    bucket,
                    processed_keys,
                    source_name="backfill-worker-s3-processed",
                    selection={"symbol": symbol, "interval": interval, "ranges": repair_ranges},
                )
                if int(materialized.get("matchedRowCount") or 0) > 0:
                    return {
                        "jobType": job_type,
                        "sourcePreference": source_preference,
                        "source": "s3-processed",
                        "gapRanges": repair_ranges,
                        "processedObjects": [f"s3://{bucket}/{key}" for key in processed_keys],
                        "materializedRowCount": materialized["rowCount"],
                    }
            if source_preference == "s3-only":
                raise BackfillUnavailable("No S3 final candle objects with matching rows are available for the requested symbol, interval, and range.")

        source_interval = historical_source_interval_for(interval)
        if is_crypto_symbol(symbol):
            source_interval = interval
        timeframe = alpaca_timeframe_for_interval(source_interval)
        adjustment = historical_adjustment_from_env(os.environ)
        raw_bars = []
        for repair_range in repair_ranges:
            raw_bars.extend(fetch_alpaca_bars(symbol, repair_range["start"], repair_range["end"], feed, timeframe))
        analysis_missing_keys = {
            str(item)
            for item in (record.get("analysisMissingCandleKeys") or [])
            if item
        }
        if interval == "1D" and analysis_missing_keys:
            from alfaka.analytics.analysis_candles import canonicalize_candle_identity
            raw_bars = [
                row for row in raw_bars
                if (
                    (identity := canonicalize_candle_identity(
                        {"timestamp": row.get("t") or row.get("timestamp")}, "1D",
                    ))
                    and identity["candleKey"] in analysis_missing_keys
                )
            ]
        if not raw_bars:
            if job_type == "initial_load":
                manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
                marker_key = write_empty_initial_load_marker(
                    self.s3,
                    bucket,
                    manifest_prefix,
                    symbol,
                    interval,
                    start,
                    end,
                    record["requestId"],
                    reason="historical provider returned no bars",
                )
                return {
                    "rawRowCount": 0,
                    "processedRowCount": 0,
                    "materializedRowCount": 0,
                    "jobType": job_type,
                    "sourcePreference": source_preference,
                    "source": "alpaca-empty",
                    "emptyRange": True,
                    "emptyMarker": f"s3://{bucket}/{marker_key}",
                    "clickhouseCoveredBeforeLoad": clickhouse_covered,
                    "gapRanges": repair_ranges,
                    "processedObjects": [],
                }
            raise BackfillUnavailable("Historical provider returned no bars.")

        raw_prefix = os.getenv("S3_RAW_PREFIX", os.getenv("S3_PREFIX", "market-data/rebuild-20260702-lazy-v1/raw/alpaca"))
        final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
        output_format = os.getenv("S3_PROCESSED_FORMAT", "parquet").lower()
        raw_kind = "daily-bars" if interval == "1D" else "bars"

        processed_source_bars = repair_daily_bar_outliers(symbol, raw_bars, feed) if interval == "1D" else raw_bars
        repair_count = sum(1 for row in processed_source_bars if row.get("_repairSource"))

        raw_count = upload_raw_bars_to_s3(
            self.s3,
            bucket,
            raw_prefix,
            raw_kind,
            feed,
            start,
            end,
            1,
            {symbol: raw_bars},
            object_id=record["requestId"],
            partition_mode=os.getenv("S3_HISTORICAL_RAW_PARTITION_MODE", "chunk"),
            price_adjustment=adjustment,
            canonical_version=CANONICAL_VERSION,
        )
        source_candles, processed = canonical_historical_candles(
            symbol,
            processed_source_bars,
            feed=feed,
            interval=interval,
            source_interval=source_interval,
            price_adjustment=adjustment,
            completed_through=parse_time(end),
        )
        if not processed:
            raise BackfillUnavailable("Historical provider returned no completed regular-session candles.")
        source_stored_rows = persist_historical_source_candles(
            self.clickhouse_client,
            source_candles,
            source_interval=source_interval,
            interval=interval,
        )
        first_event_time = parse_time(processed[0]["timestamp"])
        request_id = record["requestId"].replace(":", "_")
        partition_key = (
            f"{final_prefix}/candles/feed={feed or 'unknown'}/interval={interval}/symbol={symbol}"
            f"/year={first_event_time:%Y}/month={first_event_time:%m}/day={first_event_time:%d}"
            f"/backfill_request={request_id}"
        )
        processed_key = flush_buffer(
            self.s3,
            bucket,
            partition_key,
            processed,
            output_format,
            manifest_prefix=os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX),
            manifest_layout=os.getenv("S3_HISTORICAL_PROCESSED_MANIFEST_LAYOUT", "compact"),
            force=force_refresh,
        )
        materialized = materialize_s3_processed_objects(self.clickhouse_client, self.s3, bucket, [processed_key], source_name="backfill-worker")

        return {
            "rawRowCount": raw_count,
            "processedRowCount": len(processed),
            "materializedRowCount": materialized["rowCount"],
            "jobType": job_type,
            "sourcePreference": source_preference,
            "source": "alpaca",
            "clickhouseCoveredBeforeLoad": clickhouse_covered,
            "gapRanges": repair_ranges,
            "processedObjects": [f"s3://{bucket}/{processed_key}"],
            "dailyBarRepairCount": repair_count,
            "sourceStoredRowCount": source_stored_rows,
        }

    def _run_analysis_s3_repair(
        self,
        bucket,
        symbol,
        interval,
        repair_ranges,
        *,
        deadline_monotonic=None,
        cancel_check=None,
        lookup_metrics=None,
        prepare_only=False,
    ):
        """Materialize one request-scoped daily repair batch from historical S3."""
        started = time.monotonic()
        metrics = lookup_metrics if isinstance(lookup_metrics, dict) else {}
        for name in (
            "listCalls", "objectsListed", "manifestObjectsRead",
            "objectsSelected", "objectGets",
        ):
            metrics.setdefault(name, 0)
        ranges = [
            {"start": item.get("start"), "end": item.get("end"), "candleKeys": list(item.get("candleKeys") or [])}
            for item in repair_ranges
            if isinstance(item, dict) and item.get("start") and item.get("end")
        ]
        if not ranges:
            raise BackfillUnavailable("Analysis repair requires at least one bounded range.")

        def check_deadline():
            if callable(cancel_check) and cancel_check():
                exc = BackfillUnavailable("Analysis repair was canceled.")
                exc.metrics = _finalize_lookup_metrics(metrics, started)
                raise exc
            if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
                raise BackfillDeadlineExceeded(
                    "S3 analysis repair exceeded its stage deadline.",
                    metrics=_finalize_lookup_metrics(metrics, started),
                )

        try:
            check_deadline()
            manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
            processed_keys = analysis_repair_processed_candle_keys(
                self.s3,
                bucket,
                manifest_prefix,
                symbol,
                interval,
                ranges,
                metrics=metrics,
                deadline_check=check_deadline,
            )
            check_deadline()
            if not processed_keys:
                exc = BackfillUnavailable(
                    "No S3 final candle objects with matching rows are available for the analysis repair ranges."
                )
                exc.metrics = _finalize_lookup_metrics(metrics, started)
                raise exc
            prepared = prepare_s3_processed_objects(
                self.clickhouse_client,
                self.s3,
                bucket,
                processed_keys,
                selection={"symbol": symbol, "interval": interval, "ranges": ranges},
                metrics=metrics,
                deadline_check=check_deadline,
                # A load-audit row proves that this object was handled before;
                # it does not prove that every requested canonical candle is
                # still present. Readiness repair exists specifically to heal
                # rows that can be missing while that audit survives.
                force_rematerialize=True,
                # Shared compact objects can contain other symbols and dates.
                # Request-scoped repair must never refresh rows outside the
                # audited missing ranges from an older S3 snapshot.
                filter_to_selection=True,
            )
            matched_rows = int(prepared.get("matchedRowCount") or 0)
            if matched_rows <= 0:
                exc = BackfillUnavailable(
                    "No S3 final candle objects with matching rows are available for the analysis repair ranges."
                )
                exc.metrics = _finalize_lookup_metrics(metrics, started)
                raise exc
            finalized_metrics = _finalize_lookup_metrics(metrics, started)
            result = {
                "jobType": "gapfill",
                "sourcePreference": "s3-only",
                "source": "s3-processed",
                "gapRanges": ranges,
                "processedObjectCount": len(processed_keys),
                "materializedRowCount": matched_rows,
                "lookupMetrics": finalized_metrics,
            }
            if prepare_only:
                result["_preparedMaterialization"] = prepared
                return result
            committed = commit_prepared_s3_processed_objects(
                self.clickhouse_client,
                prepared,
                source_name="chart-analysis-repair-s3-processed",
                write_object_audits=False,
            )
            result["materialization"] = committed
            return result
        except BackfillDeadlineExceeded:
            raise
        except BackfillUnavailable:
            raise
        except Exception as exc:
            if (
                (deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic))
                or _is_timeout_exception(exc)
            ):
                raise BackfillDeadlineExceeded(
                    "S3 analysis repair exceeded its stage deadline.",
                    metrics=_finalize_lookup_metrics(metrics, started),
                ) from exc
            wrapped = BackfillUnavailable(f"S3 analysis repair failed: {exc}")
            wrapped.metrics = _finalize_lookup_metrics(metrics, started)
            raise wrapped from exc

    def detect_missing_ranges(self, symbol, interval, start, end, job_type):
        """ClickHouse에 이미 있는 timestamp를 보고 실제로 비어 있는 gap 구간만 계산합니다."""
        if job_type != "gapfill" or os.getenv("BACKFILL_GAPFILL_DETECT_INTERNAL", "true").lower() not in {"1", "true", "yes"}:
            return None
        if not hasattr(self.coverage_provider, "candle_timestamps"):
            return None
        if not internal_gap_detection_allowed(interval, start, end):
            return None
        actual_timestamps = self.coverage_provider.candle_timestamps(
            symbol,
            interval,
            start,
            end,
            limit=int(os.getenv("BACKFILL_GAPFILL_TIMESTAMP_LIMIT", "200000")),
        )
        return [
            {"start": gap.start, "end": gap.end, "missingCount": gap.missingCount}
            for gap in detect_gapfill_ranges(start, end, interval, actual_timestamps, calendar=calendar_for_symbol(symbol))
        ]

    def _run_replay_job(self, bucket, symbol, interval, start, end, job_type, source_preference):
        """S3에 저장된 processed/raw 객체를 재생해서 ClickHouse를 복구합니다."""
        if source_preference == "alpaca-only":
            raise BackfillUnavailable(f"{job_type} cannot use sourcePreference=alpaca-only.")
        final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/rebuild-20260702-lazy-v1/final"))
        processed_keys = find_processed_candle_objects(self.s3, bucket, final_prefix, symbol, interval, start, end)
        if processed_keys:
            materialized = materialize_s3_processed_objects(
                self.clickhouse_client,
                self.s3,
                bucket,
                processed_keys,
                source_name=f"backfill-worker-{job_type}-processed",
                selection={"symbol": symbol, "interval": interval, "start": start, "end": end},
            )
            if int(materialized.get("matchedRowCount") or 0) > 0:
                return {
                    "jobType": job_type,
                    "sourcePreference": source_preference,
                    "source": "s3-processed-replay",
                    "processedObjects": [f"s3://{bucket}/{key}" for key in processed_keys],
                    "materializedRowCount": materialized["rowCount"],
                }

        raise BackfillUnavailable(f"No S3 final candle objects with matching rows are available for {job_type}.")


def fetch_alpaca_bars(symbol, start, end, feed, timeframe="1Min"):
    """Alpaca historical bars API에서 주식 또는 crypto 캔들을 가져옵니다."""
    import requests
    from alfaka.common.secrets import load_alpaca_credentials

    key, secret = load_alpaca_credentials()
    if not key or not secret:
        raise BackfillUnavailable("Alpaca credentials are not configured.")

    provider_symbol = alpaca_provider_symbol(symbol)
    crypto_symbol = is_crypto_symbol(symbol)
    endpoint = (
        f"https://data.alpaca.markets/v1beta3/crypto/{os.getenv('ALPACA_CRYPTO_LOCATION', 'us')}/bars"
        if crypto_symbol
        else "https://data.alpaca.markets/v2/stocks/bars"
    )
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "symbols": provider_symbol,
        "start": start,
        "end": end,
        "timeframe": timeframe,
        "limit": os.getenv("HISTORICAL_LIMIT", "10000"),
        "sort": "asc",
    }
    adjustment = historical_adjustment_from_env(os.environ)
    if not crypto_symbol:
        params["feed"] = feed
        params["adjustment"] = adjustment
    max_attempts = max(1, int(os.getenv("HISTORICAL_MAX_RETRIES", "5")))
    retry_sleep_seconds = max(0.0, float(os.getenv("HISTORICAL_RETRY_SLEEP_SECONDS", "1")))
    retry_max_sleep_seconds = max(retry_sleep_seconds, float(os.getenv("HISTORICAL_RETRY_MAX_SLEEP_SECONDS", "30")))
    rows = []
    while True:
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(endpoint, headers=headers, params=params, timeout=30)
            except requests.RequestException as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(f"Alpaca historical request failed after retries: {exc}") from exc
                time.sleep(historical_retry_delay(None, attempt, retry_sleep_seconds, retry_max_sleep_seconds))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= max_attempts:
                    raise RuntimeError(f"Alpaca historical request failed: status={response.status_code}, body={response.text}")
                time.sleep(historical_retry_delay(response, attempt, retry_sleep_seconds, retry_max_sleep_seconds))
                continue
            break
        if response.status_code >= 400:
            raise RuntimeError(f"Alpaca historical request failed: status={response.status_code}, body={response.text}")
        payload = response.json()
        rows.extend((payload.get("bars") or {}).get(provider_symbol, []))
        page_token = payload.get("next_page_token")
        if not page_token:
            return rows
        params["page_token"] = page_token


def historical_retry_delay(response, attempt, base_seconds, max_seconds):
    """Alpaca rate limit이나 일시 오류 재시도 전에 기다릴 시간을 계산합니다."""
    retry_after = None
    if response is not None:
        retry_after = getattr(response, "headers", {}).get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), max_seconds)
        except ValueError:
            pass
    return min(base_seconds * (2 ** max(attempt - 1, 0)), max_seconds)


def repair_daily_bar_outliers(symbol, raw_bars, feed):
    """주식 일봉의 비정상 high/low를 1분봉 재집계로 보정합니다."""
    if is_crypto_symbol(symbol):
        return raw_bars
    repaired = []
    for raw_bar in raw_bars:
        outlier_flags = daily_bar_outlier_flags(raw_bar)
        if not any(outlier_flags.values()):
            repaired.append(raw_bar)
            continue
        minute_rows = fetch_alpaca_bars(symbol, raw_bar.get("t"), daily_bar_repair_end(raw_bar.get("t")), feed, "1Min")
        aggregate = aggregate_minute_bars_to_daily(minute_rows)
        if not aggregate:
            repaired.append(raw_bar)
            continue
        patched = dict(raw_bar)
        if outlier_flags["high"]:
            patched["h"] = aggregate["h"]
        if outlier_flags["low"]:
            patched["l"] = aggregate["l"]
        repaired.append({
            **patched,
            "_repairSource": "alpaca.1m-bars",
            "_originalDailyBar": {key: raw_bar.get(key) for key in ("o", "h", "l", "c", "v", "n", "vw", "t")},
        })
    return repaired


def daily_bar_requires_1m_validation(raw_bar):
    return any(daily_bar_outlier_flags(raw_bar).values())


def daily_bar_outlier_flags(raw_bar):
    if os.getenv("DAILY_BAR_1M_REPAIR_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return {"high": False, "low": False}
    high = float_or_none(raw_bar.get("h"))
    low = float_or_none(raw_bar.get("l"))
    references = [float_or_none(raw_bar.get(key)) for key in ("o", "c", "vw")]
    references = [value for value in references if value and value > 0]
    if not high or not low or low <= 0 or not references:
        return {"high": True, "low": True}
    threshold = max(1.0, float(os.getenv("DAILY_BAR_1M_REPAIR_RATIO", "1.5")))
    return {
        "high": high / max(references) > threshold,
        "low": min(references) / low > threshold,
    }


def daily_bar_repair_end(start):
    return iso_utc(parse_time(start) + timedelta(days=1))


def aggregate_minute_bars_to_daily(rows):
    """1분봉 목록을 하나의 일봉 값으로 집계합니다."""
    rows = sorted([row for row in rows if row.get("t")], key=lambda row: row["t"])
    if not rows:
        return None
    total_volume = sum(float(row.get("v") or 0) for row in rows)
    weighted_vwap = sum(float(row.get("vw") or row.get("c") or 0) * float(row.get("v") or 0) for row in rows)
    return {
        "o": rows[0].get("o"),
        "h": max(row.get("h") for row in rows if row.get("h") is not None),
        "l": min(row.get("l") for row in rows if row.get("l") is not None),
        "c": rows[-1].get("c"),
        "v": total_volume,
        "n": sum(int(row.get("n") or 0) for row in rows),
        "vw": round(weighted_vwap / total_volume, 6) if total_volume else rows[-1].get("vw"),
    }


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_source_preference(value):
    normalized = (value or "coverage-first").strip().lower().replace("_", "-")
    allowed = {"coverage-first", "alpaca-only", "s3-only"}
    if normalized not in allowed:
        raise BackfillUnavailable(f"Unsupported backfill sourcePreference: {value}.")
    return normalized


def historical_feed_for_symbol(symbol, default_feed):
    """심볼 종류에 맞는 historical feed 이름을 반환합니다."""
    if not is_crypto_symbol(symbol):
        return default_feed
    return f"crypto-{os.getenv('ALPACA_CRYPTO_LOCATION', 'us')}"


def calendar_for_symbol(symbol):
    """gapfill 계산에 사용할 심볼별 거래 캘린더를 선택합니다."""
    return TradingCalendar.crypto_24x7() if is_crypto_symbol(symbol) else None


def range_is_covered_by_clickhouse(coverage, start, end):
    if not coverage:
        return False
    available_from = coverage.get("availableFrom")
    available_to = coverage.get("availableTo")
    row_count = int(coverage.get("rowCount") or 0)
    if not available_from or not available_to or row_count <= 0:
        return False
    return str(available_from) <= str(start) and str(available_to) >= str(end)


def find_processed_candle_objects(s3, bucket, final_prefix, symbol, interval, start, end):
    from alfaka.storage.s3_manifest import bounded_v2_processed_candle_keys

    manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
    manifest_keys = processed_candle_keys_from_manifest(s3, bucket, manifest_prefix, symbol, interval, start, end)
    v2_keys = bounded_v2_processed_candle_keys(s3, bucket, final_prefix, symbol, interval, start, end)
    if manifest_keys or v2_keys:
        return list(dict.fromkeys([*manifest_keys, *v2_keys]))
    if require_canonical_processed_manifest():
        return []
    return bounded_processed_candle_partition_keys(s3, bucket, final_prefix, symbol, interval, start, end)


def internal_gap_detection_allowed(interval, start, end):
    interval = normalize_chart_interval(interval)
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    max_days = int(os.getenv(
        "BACKFILL_GAPFILL_MAX_DETECT_DAYS",
        "7" if interval == "1m" else "1500",
    ))
    return end_dt - start_dt <= timedelta(days=max_days)


def unique_ordered(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _finalize_lookup_metrics(metrics, started):
    result = dict(metrics or {})
    result["elapsedMs"] = max(0, int(round((time.monotonic() - started) * 1000)))
    return result


def _is_timeout_exception(exc):
    return "timeout" in exc.__class__.__name__.lower() or isinstance(exc, TimeoutError)


def upload_raw_bars_to_s3(
    s3,
    bucket,
    prefix,
    data_kind,
    feed,
    start,
    end,
    page_number,
    rows_by_symbol,
    object_id=None,
    partition_mode="chunk",
    price_adjustment=None,
    canonical_version=None,
):
    from alfaka.storage.raw_s3_archive import upload_raw_page_to_s3

    return upload_raw_page_to_s3(
        s3,
        bucket,
        prefix,
        data_kind,
        feed,
        start,
        end,
        page_number,
        rows_by_symbol,
        manifest_prefix=os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX),
        object_id=object_id,
        partition_mode=partition_mode,
        price_adjustment=price_adjustment,
        canonical_version=canonical_version,
    )


def write_empty_initial_load_marker(s3, bucket, manifest_prefix, symbol, interval, start, end, request_id, reason):
    safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(request_id)).strip("._-")[:120]
    key = (
        f"{manifest_prefix.strip('/')}/empty/candles/interval={interval}/symbol={symbol}"
        f"/request={safe_request_id}.json"
    )
    body = json.dumps(
        {
            "schemaVersion": 1,
            "dataset": "candles",
            "symbol": symbol,
            "interval": interval,
            "range": {"start": start, "end": end},
            "requestId": request_id,
            "emptyRange": True,
            "reason": reason,
            "createdAt": utc_now_iso(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return key


def raw_bar_to_processed_candle(symbol, raw_bar, feed="sip", received_at=None, interval="1m", price_adjustment=None):
    """Alpaca historical bar 하나를 ClickHouse/S3용 processed candle로 변환합니다."""
    interval = normalize_chart_interval(interval)
    price_adjustment = price_adjustment or historical_adjustment_from_env(os.environ)
    received_at = received_at or utc_now_iso()
    channel = "dailyBars" if interval == "1D" else "bars"
    message_type = "d" if interval == "1D" else "b"
    message = {"T": message_type, "S": symbol, **raw_bar}
    repair_suffix = "/repair=1m" if raw_bar.get("_repairSource") else ""
    event_id = f"{source_event_id(message, feed, channel, symbol, received_at)}/adjustment={price_adjustment}{repair_suffix}"
    metadata = candle_metadata(price_adjustment, CANONICAL_VERSION)
    envelope = {
        "source": "alpaca",
        "feed": feed,
        "feedProfile": feed,
        "marketSession": (
            "crypto" if is_crypto_symbol(symbol)
            else "regular" if interval in {"1D", "1W", "1M"}
            else market_session_for_timestamp(raw_bar.get("t"))
        ),
        "channel": channel,
        "symbol": symbol,
        "eventTime": raw_bar.get("t"),
        "receivedAt": received_at,
        "sourceEventId": event_id,
        "raw": raw_bar,
        **metadata,
    }
    candle = normalize_bar(envelope)
    candle["interval"] = interval
    if raw_bar.get("_repairSource"):
        candle["source"] = "alpaca.dailyBars.repairedFrom1m"
        candle["correctionType"] = "REPAIRED"
        candle["repairSource"] = raw_bar.get("_repairSource")
    return candle


def raw_bars_to_processed_candles(symbol, raw_bars, feed="sip", interval="1m", price_adjustment=None):
    """여러 historical bar를 processed candle 목록으로 변환하고 이동평균을 붙입니다."""
    return attach_moving_averages([
        raw_bar_to_processed_candle(symbol, row, feed=feed, interval=interval, price_adjustment=price_adjustment)
        for row in raw_bars
    ])


def canonical_historical_candles(
    symbol,
    raw_bars,
    *,
    feed="sip",
    interval="1m",
    source_interval=None,
    price_adjustment=None,
    completed_through=None,
):
    """실제 세션 원본 봉과 요청 interval의 canonical 봉을 함께 만든다."""
    target_interval = normalize_chart_interval(interval)
    resolved_source = normalize_chart_interval(
        source_interval
        or (target_interval if is_crypto_symbol(symbol) else historical_source_interval_for(target_interval))
    )
    source_candles = raw_bars_to_processed_candles(
        symbol,
        raw_bars,
        feed=feed,
        interval=resolved_source,
        price_adjustment=price_adjustment,
    )
    can_aggregate_intraday = (
        target_interval in INTRADAY_DERIVED_INTERVALS
        and resolved_source in INTRADAY_INTERVAL_MINUTES
        and INTRADAY_INTERVAL_MINUTES[resolved_source] < INTRADAY_INTERVAL_MINUTES[target_interval]
        and not is_crypto_symbol(symbol)
    )
    if not can_aggregate_intraday:
        return source_candles, source_candles
    regular_source = [
        candle for candle in source_candles
        if candle.get("marketSession") in {None, "", "regular"}
    ]
    regular_derived = aggregate_regular_session_candles(
        regular_source,
        target_interval,
        now=completed_through,
        source_interval=resolved_source,
    )
    extended_derived = aggregate_extended_session_candles(
        source_candles,
        target_interval,
        now=completed_through,
        source_interval=resolved_source,
    )
    derived = sorted(
        [*regular_derived, *extended_derived],
        key=lambda candle: candle.get("timestamp") or "",
    )
    return source_candles, attach_moving_averages(derived)


def persist_historical_source_candles(client, source_candles, *, source_interval, interval):
    """파생 봉의 근거가 된 실제 intraday 원본 봉을 ClickHouse에 직접 보존한다."""
    source_interval = normalize_chart_interval(source_interval)
    interval = normalize_chart_interval(interval)
    if (
        source_interval == interval
        or source_interval not in INTRADAY_INTERVAL_MINUTES
        or interval not in INTRADAY_DERIVED_INTERVALS
        or INTRADAY_INTERVAL_MINUTES[source_interval] >= INTRADAY_INTERVAL_MINUTES[interval]
    ):
        return 0
    rows = [
        candle_to_clickhouse_row({**candle, "bucketPolicy": BUCKET_POLICY_SOURCE_NATIVE})
        for candle in source_candles
    ]
    if rows:
        client.insert_json_each_row("chart_candles", rows)
    return len(rows)


def main():
    from alfaka.backfill.status import RedisBackfillStore

    load_dotenv()
    request_json = os.getenv("BACKFILL_REQUEST_JSON")
    if not request_json:
        raise SystemExit("BACKFILL_REQUEST_JSON is required.")
    store = RedisBackfillStore()
    record = json.loads(request_json)
    result = BackfillRunner(store=store).run(record)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
