import os
import time
from datetime import timedelta

from alfaka.backfill.gapfill import canonical_daily_timestamp, detect_gapfill_ranges, expected_bucket_starts, parse_time, to_iso
from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.market_messages import source_event_id
from alfaka.alpaca.feed_profiles import market_session_for_timestamp
from alfaka.serving.intervals import is_derived_interval, normalize_chart_interval, source_interval_for
from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider
from alfaka.serving.history_window import clamp_range_start, range_ends_before_history
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient
from alfaka.storage.clickhouse_materializer import materialize_prepared_processed_rows, prepare_processed_candle_rows
from alfaka.storage.processed_s3_archive import archive_processed_candles_to_s3
from alfaka.storage.s3_prefixes import default_s3_archive_prefix, first_configured_prefix
from alfaka.streaming.transforms import normalize_bar


class BackfillUnavailable(RuntimeError):
    pass


class BackfillRunner:
    def __init__(self, store=None, clickhouse_client=None, coverage_provider=None, s3_client=None):
        load_dotenv()
        self.store = store
        self.s3_client = s3_client
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
        symbol = record["symbol"]
        interval = normalize_chart_interval(record["interval"])
        start = record["range"]["start"]
        end = record["range"]["end"]
        job_type = (record.get("jobType") or "gapfill").strip().lower()
        source_preference = normalize_source_preference(record.get("sourcePreference", "coverage-first"))
        force = bool(record.get("force"))
        feed = os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip"))

        if job_type != "gapfill":
            raise BackfillUnavailable(f"Unsupported backfill job type: {job_type}.")
        if is_derived_interval(interval):
            source_interval = source_interval_for(interval)
            raise BackfillUnavailable(
                f"Backfill for {interval} is derived from {source_interval}; request {source_interval} backfill first."
            )
        if interval not in {"1m", "1D"}:
            raise BackfillUnavailable("Backfill v1 supports direct 1m and 1D historical bars.")

        if range_ends_before_history(end, interval):
            history_floor = clamp_range_start(start, interval)[2]
            no_data_before = history_floor
            if self.store and hasattr(self.store, "record_no_data_before"):
                no_data_before = self.store.record_no_data_before(symbol, interval, history_floor)
            return {
                "rawRowCount": 0,
                "processedRowCount": 0,
                "materializedRowCount": 0,
                "jobType": job_type,
                "sourcePreference": source_preference,
                "source": "history-window",
                "emptyRange": True,
                "noDataBefore": no_data_before,
                "reason": "requested range is older than the configured Alpaca historical window",
                "clickhouseCoveredBeforeLoad": False,
                "gapRanges": [],
                "fetchRanges": [],
            }

        start, history_clamped, history_floor = clamp_range_start(start, interval)
        history_no_data_before = None
        if history_clamped and history_floor:
            history_no_data_before = history_floor
            if self.store and hasattr(self.store, "record_no_data_before"):
                history_no_data_before = self.store.record_no_data_before(symbol, interval, history_floor)

        if range_has_no_expected_buckets(start, end, interval):
            return {
                "rawRowCount": 0,
                "processedRowCount": 0,
                "materializedRowCount": 0,
                "jobType": job_type,
                "sourcePreference": source_preference,
                "source": "calendar-empty",
                "emptyRange": True,
                "reason": "requested range contains no expected market-data buckets",
                "noDataBefore": history_no_data_before,
                "clickhouseCoveredBeforeLoad": False,
                "gapRanges": [],
                "fetchRanges": [],
            }

        coverage = None
        repair_ranges = [{"start": start, "end": end, "missingCount": None}]
        clickhouse_covered = False
        if source_preference == "coverage-first" and not force:
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
            elif range_is_covered_by_clickhouse(coverage, start, end, interval):
                clickhouse_covered = True
                return {
                    "jobType": job_type,
                    "sourcePreference": source_preference,
                    "source": "clickhouse",
                    "skipped": True,
                    "reason": "requested range is already covered in ClickHouse",
                    "coverage": coverage,
                }

        timeframe = "1Day" if interval == "1D" else "1Min"
        fetch_ranges = alpaca_fetch_ranges(interval, repair_ranges)
        raw_bars = []
        for fetch_range in fetch_ranges:
            raw_bars.extend(fetch_alpaca_bars(symbol, fetch_range["start"], fetch_range["end"], feed, timeframe))
        leading_missing_edge = leading_missing_edge_start(start, repair_ranges, interval)
        if not raw_bars:
            if range_has_no_expected_buckets(start, end, interval):
                return {
                    "rawRowCount": 0,
                    "processedRowCount": 0,
                    "materializedRowCount": 0,
                    "jobType": job_type,
                    "sourcePreference": source_preference,
                    "source": "calendar-empty",
                    "emptyRange": True,
                    "reason": "requested range contains no expected market-data buckets",
                    "clickhouseCoveredBeforeLoad": clickhouse_covered,
                    "gapRanges": repair_ranges,
                    "fetchRanges": fetch_ranges,
                }
            no_data_before = None
            if leading_missing_edge:
                no_data_before = end
                if self.store and hasattr(self.store, "record_no_data_before"):
                    no_data_before = self.store.record_no_data_before(symbol, interval, end)
            return {
                "rawRowCount": 0,
                "processedRowCount": 0,
                "materializedRowCount": 0,
                "jobType": job_type,
                "sourcePreference": source_preference,
                "source": "alpaca-empty",
                "emptyRange": True,
                "noDataBefore": history_no_data_before or no_data_before,
                "leadingMissingEdge": leading_missing_edge,
                "clickhouseCoveredBeforeLoad": clickhouse_covered,
                "gapRanges": repair_ranges,
                "fetchRanges": fetch_ranges,
            }

        partial_boundary = partial_history_boundary(
            symbol,
            interval,
            leading_missing_edge,
            raw_bars,
            self.store,
        )
        processed = raw_bars_to_processed_candles(symbol, raw_bars, feed=feed, interval=interval)
        source_path = f"alpaca://{symbol}/{interval}/{record['requestId']}"
        prepared = prepare_processed_candle_rows(processed)
        materialized = materialize_prepared_processed_rows(
            self.clickhouse_client,
            source_path,
            prepared["rows"],
            source_name="backfill-worker-alpaca",
            skipped_invalid=prepared["skippedInvalidRowCount"],
        )
        archive = self.archive_processed_candles(prepared["rows"])

        return {
            "rawRowCount": len(raw_bars),
            "processedRowCount": len(processed),
            "materializedRowCount": materialized["rowCount"],
            "skippedInvalidRowCount": materialized.get("skippedInvalidRowCount", 0),
            **archive,
            "jobType": job_type,
            "sourcePreference": source_preference,
            "force": force,
            "source": "alpaca",
            "clickhouseCoveredBeforeLoad": clickhouse_covered,
            **with_history_boundary(partial_boundary, history_no_data_before),
            "gapRanges": repair_ranges,
            "fetchRanges": fetch_ranges,
            "materializedSource": materialized["objectPath"],
        }

    def archive_processed_candles(self, rows):
        if not rows:
            return {"archiveStatus": "skipped", "archiveReason": "no valid rows"}
        if os.getenv("BACKFILL_S3_ARCHIVE_ENABLED", "true").lower() not in {"1", "true", "yes", "y", "on"}:
            return {"archiveStatus": "skipped", "archiveReason": "disabled"}
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            return {"archiveStatus": "skipped", "archiveReason": "S3_BUCKET not configured"}

        try:
            s3 = self.s3_client
            if s3 is None:
                from alfaka.common.s3_client import create_s3_client
                s3 = create_s3_client()
            result = archive_processed_candles_to_s3(
                s3,
                bucket,
                first_configured_prefix(
                    ["S3_BACKFILL_PROCESSED_PREFIX"],
                    default_s3_archive_prefix("backfill_processed"),
                ),
                rows,
                output_format=os.getenv("S3_BACKFILL_PROCESSED_FORMAT", "jsonl").strip().lower(),
                manifest_prefix=first_configured_prefix(
                    ["S3_BACKFILL_MANIFEST_PREFIX", "S3_MANIFEST_PREFIX"],
                    default_s3_archive_prefix("manifest"),
                ),
                manifest_layout=os.getenv("S3_BACKFILL_PROCESSED_MANIFEST_LAYOUT", "compact"),
                max_attempts=max(1, int(os.getenv("S3_PUT_MAX_ATTEMPTS", "3"))),
                retry_sleep_seconds=max(0.0, float(os.getenv("S3_PUT_RETRY_SLEEP_SECONDS", "1"))),
                rows_per_object=positive_int_env("S3_BACKFILL_ARCHIVE_ROWS_PER_OBJECT", 10000),
            )
            return {
                "archiveStatus": "archived",
                "archiveRowCount": result["rowCount"],
                "archiveObjectCount": result["objectCount"],
                "archiveObjects": result["objectKeys"],
            }
        except Exception as exc:
            return {
                "archiveStatus": "failed",
                "archiveRowCount": 0,
                "archiveObjectCount": 0,
                "archiveError": str(exc),
            }

    def detect_missing_ranges(self, symbol, interval, start, end, job_type):
        if job_type != "gapfill" or os.getenv("BACKFILL_GAPFILL_DETECT_INTERNAL", "true").lower() not in {"1", "true", "yes"}:
            return None
        if not hasattr(self.coverage_provider, "candle_timestamps"):
            return None
        ranges = []
        for chunk_start, chunk_end in internal_gap_detection_chunks(interval, start, end):
            actual_timestamps = self.coverage_provider.candle_timestamps(
                symbol,
                interval,
                chunk_start,
                chunk_end,
                limit=int(os.getenv("BACKFILL_GAPFILL_TIMESTAMP_LIMIT", "200000")),
            )
            ranges.extend(
                {"start": gap.start, "end": gap.end, "missingCount": gap.missingCount}
                for gap in detect_gapfill_ranges(chunk_start, chunk_end, interval, actual_timestamps)
            )
        return ranges


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
    adjustment = os.getenv("HISTORICAL_ADJUSTMENT", "split").strip()
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


def normalize_source_preference(value):
    normalized = (value or "coverage-first").strip().lower().replace("_", "-")
    allowed = {"coverage-first", "alpaca-only"}
    if normalized not in allowed:
        raise BackfillUnavailable(f"Unsupported backfill sourcePreference: {value}.")
    return normalized


def alpaca_fetch_ranges(interval, repair_ranges):
    interval = normalize_chart_interval(interval)
    normalized = normalize_repair_ranges(repair_ranges)
    if not normalized:
        return []
    if interval == "1D":
        return coalesce_daily_fetch_ranges(normalized)
    if interval == "1m":
        return coalesce_intraday_fetch_ranges(normalized)
    return normalized


def normalize_repair_ranges(repair_ranges):
    normalized = []
    for item in repair_ranges or []:
        start = item.get("start") if isinstance(item, dict) else None
        end = item.get("end") if isinstance(item, dict) else None
        if not start or not end:
            continue
        normalized.append({"start": to_iso(parse_time(start)), "end": to_iso(parse_time(end))})
    return sorted(normalized, key=lambda item: item["start"])


def coalesce_daily_fetch_ranges(ranges):
    max_days = positive_int_env("BACKFILL_DAILY_FETCH_MAX_DAYS", 1500)
    max_delta = timedelta(days=max(1, max_days))
    return coalesce_fetch_ranges_by_delta(ranges, max_delta)


def coalesce_intraday_fetch_ranges(ranges):
    max_days = positive_int_env("BACKFILL_INTRADAY_FETCH_MAX_DAYS", 30)
    max_delta = timedelta(days=max(1, max_days))
    return coalesce_fetch_ranges_by_delta(ranges, max_delta)


def coalesce_fetch_ranges_by_delta(ranges, max_delta):
    coalesced = []
    current = None
    for item in ranges:
        start_dt = parse_time(item["start"])
        end_dt = parse_time(item["end"])
        if current is None:
            current = {"start": start_dt, "end": end_dt}
            continue
        proposed_end = max(current["end"], end_dt)
        if proposed_end - current["start"] <= max_delta:
            current["end"] = proposed_end
            continue
        coalesced.append({"start": to_iso(current["start"]), "end": to_iso(current["end"])})
        current = {"start": start_dt, "end": end_dt}
    if current is not None:
        coalesced.append({"start": to_iso(current["start"]), "end": to_iso(current["end"])})
    return split_fetch_ranges(coalesced, max_delta)


def split_fetch_ranges(ranges, max_delta):
    chunks = []
    for item in ranges:
        start = parse_time(item["start"])
        end = parse_time(item["end"])
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + max_delta, end)
            chunks.append({"start": to_iso(cursor), "end": to_iso(chunk_end)})
            cursor = chunk_end
    return chunks


def range_is_covered_by_clickhouse(coverage, start, end, interval):
    if not coverage:
        return False
    available_from = coverage.get("availableFrom")
    available_to = coverage.get("availableTo")
    row_count = int(coverage.get("rowCount") or 0)
    if not available_from or not available_to or row_count <= 0:
        return False
    try:
        expected = expected_bucket_starts(start, end, interval)
        if not expected:
            return True
        available_from_dt = parse_time(available_from)
        available_to_dt = parse_time(available_to)
    except (TypeError, ValueError):
        return False
    return available_from_dt <= expected[0] and available_to_dt >= expected[-1]


def internal_gap_detection_chunks(interval, start, end):
    interval = normalize_chart_interval(interval)
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    max_days = gapfill_max_detect_days(interval)
    max_delta = timedelta(days=max(1, max_days))
    cursor = start_dt
    while cursor < end_dt:
        chunk_end = min(cursor + max_delta, end_dt)
        yield to_iso(cursor), to_iso(chunk_end)
        cursor = chunk_end


def gapfill_max_detect_days(interval):
    interval = normalize_chart_interval(interval)
    if interval == "1D":
        return positive_int_env("BACKFILL_GAPFILL_MAX_DETECT_DAYS_DAILY", 1500)
    return positive_int_env("BACKFILL_GAPFILL_MAX_DETECT_DAYS_INTRADAY", 7)


def positive_int_env(name, default):
    try:
        value = int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def range_has_no_expected_buckets(start, end, interval):
    return not expected_bucket_starts(start, end, interval)


def leading_missing_edge_start(start, repair_ranges, interval):
    normalized = normalize_repair_ranges(repair_ranges)
    if not normalized:
        return None
    first_missing_start = normalized[0]["start"]
    try:
        expected_before_first_missing = expected_bucket_starts(start, first_missing_start, interval)
    except (TypeError, ValueError):
        return None
    return first_missing_start if not expected_before_first_missing else None


def partial_history_boundary(symbol, interval, leading_missing_edge, raw_bars, store=None):
    if not leading_missing_edge:
        return {}
    earliest_returned = earliest_returned_bucket(raw_bars, interval)
    if not earliest_returned or parse_time(earliest_returned) <= parse_time(leading_missing_edge):
        return {}
    missing_before = expected_bucket_starts(leading_missing_edge, earliest_returned, interval)
    if not missing_before:
        return {}
    no_data_before = earliest_returned
    if store and hasattr(store, "record_no_data_before"):
        no_data_before = store.record_no_data_before(symbol, interval, earliest_returned)
    return {
        "partialHistoryBoundary": True,
        "noDataBefore": no_data_before,
        "leadingMissingEdge": leading_missing_edge,
        "earliestReturned": earliest_returned,
        "missingBeforeCount": len(missing_before),
    }


def with_history_boundary(result, history_no_data_before):
    if not history_no_data_before:
        return result
    if not result:
        return {"partialHistoryBoundary": True, "noDataBefore": history_no_data_before}
    no_data_before = result.get("noDataBefore")
    if not no_data_before or parse_time(history_no_data_before) > parse_time(no_data_before):
        return {**result, "noDataBefore": history_no_data_before}
    return result


def earliest_returned_bucket(raw_bars, interval):
    buckets = []
    for row in raw_bars or []:
        timestamp = row.get("t") if isinstance(row, dict) else None
        if not timestamp:
            continue
        buckets.append(returned_bucket_start(timestamp, interval))
    return min(buckets) if buckets else None


def returned_bucket_start(timestamp, interval):
    interval = normalize_chart_interval(interval)
    if interval == "1D":
        return canonical_daily_timestamp(timestamp)
    parsed = parse_time(timestamp)
    return to_iso(parsed.replace(second=0, microsecond=0))


def raw_bar_to_processed_candle(symbol, raw_bar, feed="sip", received_at=None, interval="1m"):
    interval = normalize_chart_interval(interval)
    received_at = received_at or utc_now_iso()
    channel = "dailyBars" if interval == "1D" else "bars"
    message_type = "d" if interval == "1D" else "b"
    message = {"T": message_type, "S": symbol, **raw_bar}
    event_id = source_event_id(message, feed, channel, symbol, received_at)
    envelope = {
        "source": "alpaca",
        "feed": feed,
        "feedProfile": feed,
        "marketSession": "regular" if interval == "1D" else market_session_for_timestamp(raw_bar.get("t")),
        "channel": channel,
        "symbol": symbol,
        "eventTime": raw_bar.get("t"),
        "receivedAt": received_at,
        "sourceEventId": event_id,
        "raw": raw_bar,
    }
    candle = normalize_bar(envelope)
    candle["interval"] = interval
    if interval == "1D":
        candle["timestamp"] = canonical_daily_timestamp(raw_bar.get("t"))
    return candle


def raw_bars_to_processed_candles(symbol, raw_bars, feed="sip", interval="1m"):
    return attach_moving_averages([raw_bar_to_processed_candle(symbol, row, feed=feed, interval=interval) for row in raw_bars])
