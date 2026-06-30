import json
import os
import re
import time
from datetime import timedelta, timezone

from alfaka.backfill.gapfill import detect_gapfill_ranges, parse_time
from alfaka.common.canonical import CANONICAL_VERSION, candle_metadata, historical_adjustment_from_env
from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.market_messages import source_event_id
from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.serving.intervals import is_derived_interval, normalize_chart_interval, source_interval_for
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient
from alfaka.storage.processed_s3_sink import flush_buffer
from alfaka.storage.s3_manifest import (
    DEFAULT_MANIFEST_PREFIX,
    bounded_raw_partition_keys,
    bounded_processed_candle_partition_keys,
    processed_candle_keys_from_manifest,
    require_canonical_processed_manifest,
    raw_keys_from_manifest,
)
from alfaka.storage.s3_materializer import materialize_processed_rows, materialize_s3_processed_objects, read_s3_rows, s3_object_already_materialized
from alfaka.streaming.transforms import normalize_bar


class BackfillUnavailable(RuntimeError):
    pass


class BackfillRunner:
    def __init__(self, store=None, s3=None, clickhouse_client=None, coverage_provider=None):
        load_dotenv()
        if s3 is None:
            from alfaka.common.s3_client import create_s3_client

            s3 = create_s3_client()
        self.store = store
        self.s3 = s3
        self.clickhouse_client = clickhouse_client or ClickHouseHttpClient(
            url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            user=os.getenv("CLICKHOUSE_USER", "alfaka"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )
        if hasattr(self.clickhouse_client, "ensure_market_data_schema"):
            self.clickhouse_client.ensure_market_data_schema()
        self.coverage_provider = coverage_provider or ClickHouseMarketDataProvider(
            url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
            database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
            user=os.getenv("CLICKHOUSE_USER", "alfaka"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "alfaka"),
        )

    def run(self, record):
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

    def _run(self, record):
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
        feed = os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip"))

        if job_type not in {"initial_load", "gapfill", "replay_repair", "correction_replay"}:
            raise BackfillUnavailable(f"Unsupported backfill job type: {job_type}.")
        if is_derived_interval(interval):
            source_interval = source_interval_for(interval)
            raise BackfillUnavailable(
                f"Backfill for {interval} is derived from {source_interval}; request {source_interval} backfill first."
            )
        if interval not in {"1m", "1D"}:
            raise BackfillUnavailable("Backfill v1 supports direct 1m and 1D historical bars.")
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
            final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/final"))
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
                materialized = materialize_s3_processed_objects(self.clickhouse_client, self.s3, bucket, processed_keys, source_name="backfill-worker-s3-processed")
                return {
                    "jobType": job_type,
                    "sourcePreference": source_preference,
                    "source": "s3-processed",
                    "gapRanges": repair_ranges,
                    "processedObjects": [f"s3://{bucket}/{key}" for key in processed_keys],
                    "materializedRowCount": materialized["rowCount"],
                }
            raw_prefix = os.getenv("S3_RAW_PREFIX", os.getenv("S3_PREFIX", "market-data/raw/alpaca"))
            raw_keys = []
            for repair_range in repair_ranges:
                raw_keys.extend(find_raw_candle_objects(
                    self.s3,
                    bucket,
                    raw_prefix,
                    symbol,
                    interval,
                    repair_range["start"],
                    repair_range["end"],
                    job_type,
                ))
            raw_keys = unique_ordered(raw_keys)
            if raw_keys:
                return materialize_raw_s3_candle_objects(
                    self.clickhouse_client,
                    self.s3,
                    bucket,
                    raw_keys,
                    interval,
                    job_type,
                    source_preference,
                    repair_ranges,
                    source_name="backfill-worker-s3-raw",
                )
            if source_preference == "s3-only":
                raise BackfillUnavailable("No processed or raw S3 candle objects are available for the requested symbol and interval.")

        timeframe = "1Day" if interval == "1D" else "1Min"
        adjustment = historical_adjustment_from_env(os.environ)
        raw_bars = []
        for repair_range in repair_ranges:
            raw_bars.extend(fetch_alpaca_bars(symbol, repair_range["start"], repair_range["end"], feed, timeframe))
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

        raw_prefix = os.getenv("S3_RAW_PREFIX", os.getenv("S3_PREFIX", "market-data/raw/alpaca"))
        final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/final"))
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
        processed = raw_bars_to_processed_candles(symbol, processed_source_bars, feed=feed, interval=interval, price_adjustment=adjustment)
        partition_key = f"{final_prefix}/candles/interval={interval}/symbol={symbol}/backfill_request={record['requestId'].replace(':', '_')}"
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
        }

    def detect_missing_ranges(self, symbol, interval, start, end, job_type):
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
            for gap in detect_gapfill_ranges(start, end, interval, actual_timestamps)
        ]

    def _run_replay_job(self, bucket, symbol, interval, start, end, job_type, source_preference):
        if source_preference == "alpaca-only":
            raise BackfillUnavailable(f"{job_type} cannot use sourcePreference=alpaca-only.")
        final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/final"))
        processed_keys = find_processed_candle_objects(self.s3, bucket, final_prefix, symbol, interval, start, end)
        if processed_keys:
            materialized = materialize_s3_processed_objects(
                self.clickhouse_client,
                self.s3,
                bucket,
                processed_keys,
                source_name=f"backfill-worker-{job_type}-processed",
            )
            return {
                "jobType": job_type,
                "sourcePreference": source_preference,
                "source": "s3-processed-replay",
                "processedObjects": [f"s3://{bucket}/{key}" for key in processed_keys],
                "materializedRowCount": materialized["rowCount"],
            }

        raw_prefix = os.getenv("S3_RAW_PREFIX", os.getenv("S3_PREFIX", "market-data/raw/alpaca"))
        raw_keys = find_raw_candle_objects(self.s3, bucket, raw_prefix, symbol, interval, start, end, job_type)
        if not raw_keys:
            raise BackfillUnavailable(f"No S3 processed or raw candle objects are available for {job_type}.")

        return materialize_raw_s3_candle_objects(
            self.clickhouse_client,
            self.s3,
            bucket,
            raw_keys,
            interval,
            job_type,
            source_preference,
            source_name=f"backfill-worker-{job_type}-raw",
        )


def fetch_alpaca_bars(symbol, start, end, feed, timeframe="1Min"):
    import requests
    from alfaka.common.secrets import load_alpaca_credentials

    key, secret = load_alpaca_credentials()
    if not key or not secret:
        raise BackfillUnavailable("Alpaca credentials are not configured.")

    endpoint = "https://data.alpaca.markets/v2/stocks/bars"
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "symbols": symbol,
        "start": start,
        "end": end,
        "feed": feed,
        "timeframe": timeframe,
        "limit": os.getenv("HISTORICAL_LIMIT", "10000"),
        "sort": "asc",
    }
    adjustment = historical_adjustment_from_env(os.environ)
    if adjustment:
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
        rows.extend((payload.get("bars") or {}).get(symbol, []))
        page_token = payload.get("next_page_token")
        if not page_token:
            return rows
        params["page_token"] = page_token


def historical_retry_delay(response, attempt, base_seconds, max_seconds):
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
    rows = sorted([row for row in rows if row.get("t")], key=lambda row: row["t"])
    if not rows:
        return None
    total_volume = sum(int(row.get("v") or 0) for row in rows)
    weighted_vwap = sum(float(row.get("vw") or row.get("c") or 0) * int(row.get("v") or 0) for row in rows)
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


def materialize_raw_s3_candle_objects(client, s3, bucket, raw_keys, interval, job_type, source_preference, gap_ranges=None, source_name="backfill-worker-raw"):
    object_path = raw_replay_object_path(bucket, raw_keys)
    raw_object_paths = [f"s3://{bucket}/{key}" for key in raw_keys]
    if s3_object_already_materialized(client, object_path):
        payload = {
            "jobType": job_type,
            "sourcePreference": source_preference,
            "source": "s3-raw-replay",
            "rawObjects": raw_object_paths,
            "processedRowCount": 0,
            "materializedRowCount": 0,
            "skippedAlreadyMaterialized": True,
        }
        if gap_ranges is not None:
            payload["gapRanges"] = gap_ranges
        return payload
    raw_rows = []
    for key in raw_keys:
        raw_rows.extend(read_s3_rows(s3, bucket, key))
    processed = raw_archive_rows_to_processed_candles(raw_rows, interval)
    if not processed:
        raise BackfillUnavailable(f"S3 raw objects did not contain replayable {interval} candle rows.")
    result = materialize_processed_rows(
        client,
        object_path,
        processed,
        source_name=source_name,
    )
    payload = {
        "jobType": job_type,
        "sourcePreference": source_preference,
        "source": "s3-raw-replay",
        "rawObjects": raw_object_paths,
        "processedRowCount": len(processed),
        "materializedRowCount": result["rowCount"],
    }
    if gap_ranges is not None:
        payload["gapRanges"] = gap_ranges
    return payload


def raw_replay_object_path(bucket, raw_keys):
    return f"s3://{bucket}/{raw_keys[0]}..{len(raw_keys)}-raw-objects"


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
    manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
    manifest_keys = processed_candle_keys_from_manifest(s3, bucket, manifest_prefix, symbol, interval, start, end)
    if manifest_keys:
        return manifest_keys
    if require_canonical_processed_manifest():
        return []
    return bounded_processed_candle_partition_keys(s3, bucket, final_prefix, symbol, interval, start, end)


def find_raw_candle_objects(s3, bucket, raw_prefix, symbol, interval, start, end, job_type="replay_repair"):
    manifest_prefix = os.getenv("S3_MANIFEST_PREFIX", DEFAULT_MANIFEST_PREFIX)
    channels = raw_channels_for_interval(interval, job_type)
    manifest_keys = raw_keys_from_manifest(s3, bucket, manifest_prefix, symbol, channels, start, end)
    if manifest_keys:
        return manifest_keys
    if require_canonical_processed_manifest():
        return []
    return bounded_raw_partition_keys(s3, bucket, raw_prefix, symbol, channels, start, end)


def raw_channels_for_interval(interval, job_type="replay_repair"):
    interval = normalize_chart_interval(interval)
    if interval == "1D":
        return ["daily-bars"]
    if job_type == "correction_replay":
        return ["updated-bars", "bars"]
    return ["bars"]


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


def raw_archive_rows_to_processed_candles(rows, interval):
    interval = normalize_chart_interval(interval)
    candles = []
    for row in sorted(rows, key=lambda item: item.get("eventTime") or (item.get("raw") or {}).get("t") or ""):
        channel = raw_archive_channel_to_envelope_channel(row.get("channel"))
        if interval == "1D" and channel != "dailyBars":
            continue
        if interval == "1m" and channel not in {"bars", "updatedBars"}:
            continue
        raw = row.get("raw") or {}
        feed = row.get("feed") or "unknown"
        feed_profile = row.get("feedProfile") or row.get("feed_profile") or feed
        market_session = row.get("marketSession") or row.get("market_session") or market_session_for_timestamp(row.get("eventTime") or raw.get("t"))
        symbol = row.get("symbol") or raw.get("S")
        received_at = row.get("receivedAt") or utc_now_iso()
        envelope = {
            "source": row.get("source", "alpaca"),
            "feed": feed,
            "feedProfile": feed_profile,
            "marketSession": market_session,
            "channel": channel,
            "symbol": symbol,
            "eventTime": row.get("eventTime") or raw.get("t"),
            "receivedAt": received_at,
            "sourceEventId": row.get("sourceEventId") or source_event_id(raw, feed, channel, symbol, received_at),
            "raw": raw,
            "priceAdjustment": row.get("priceAdjustment") or row.get("price_adjustment"),
            "canonicalVersion": row.get("canonicalVersion") or row.get("canonical_version"),
        }
        candle = normalize_bar(envelope, correction_type="UPDATED" if channel == "updatedBars" else "NONE")
        candle["interval"] = interval
        candles.append(candle)
    return attach_moving_averages(candles)


def raw_archive_channel_to_envelope_channel(channel):
    value = str(channel or "")
    return {
        "daily-bars": "dailyBars",
        "updated-bars": "updatedBars",
    }.get(value, value)


def raw_bar_to_processed_candle(symbol, raw_bar, feed="sip", received_at=None, interval="1m", price_adjustment=None):
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
        "marketSession": market_session_for_timestamp(raw_bar.get("t")),
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
    return attach_moving_averages([
        raw_bar_to_processed_candle(symbol, row, feed=feed, interval=interval, price_adjustment=price_adjustment)
        for row in raw_bars
    ])


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
