import json
import os

from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.market_messages import source_event_id
from alfaka.serving.intervals import is_derived_interval, normalize_chart_interval, source_interval_for
from alfaka.serving.moving_average import attach_moving_averages
from alfaka.storage.clickhouse_loader import ClickHouseHttpClient
from alfaka.storage.processed_s3_sink import flush_buffer
from alfaka.storage.s3_materializer import materialize_s3_processed_objects
from alfaka.streaming.transforms import normalize_bar


class BackfillUnavailable(RuntimeError):
    pass


class BackfillRunner:
    def __init__(self, store=None, s3=None, clickhouse_client=None):
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
        feed = os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip"))

        if is_derived_interval(interval):
            source_interval = source_interval_for(interval)
            raise BackfillUnavailable(
                f"Backfill for {interval} is derived from {source_interval}; request {source_interval} backfill first."
            )
        if interval not in {"1m", "1D"}:
            raise BackfillUnavailable("Backfill v1 supports direct 1m and 1D historical bars.")

        timeframe = "1Day" if interval == "1D" else "1Min"
        raw_bars = fetch_alpaca_bars(symbol, start, end, feed, timeframe)
        if not raw_bars:
            raise BackfillUnavailable("Historical provider returned no bars.")

        raw_prefix = os.getenv("S3_RAW_PREFIX", os.getenv("S3_PREFIX", "market-data/raw/alpaca"))
        final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/final"))
        output_format = os.getenv("S3_PROCESSED_FORMAT", "jsonl").lower()
        raw_kind = "daily-bars" if interval == "1D" else "bars"

        raw_count = upload_raw_bars_to_s3(self.s3, bucket, raw_prefix, raw_kind, feed, start, end, 1, {symbol: raw_bars})
        processed = raw_bars_to_processed_candles(symbol, raw_bars, feed=feed, interval=interval)
        partition_key = f"{final_prefix}/candles/interval={interval}/symbol={symbol}/backfill_request={record['requestId'].replace(':', '_')}"
        processed_key = flush_buffer(self.s3, bucket, partition_key, processed, output_format)
        materialized = materialize_s3_processed_objects(self.clickhouse_client, self.s3, bucket, [processed_key], source_name="backfill-worker")

        return {
            "rawRowCount": raw_count,
            "processedRowCount": len(processed),
            "materializedRowCount": materialized["rowCount"],
            "processedObjects": [f"s3://{bucket}/{processed_key}"],
        }


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
    rows = []
    while True:
        response = requests.get(endpoint, headers=headers, params=params, timeout=30)
        if response.status_code >= 400:
            raise RuntimeError(f"Alpaca historical request failed: status={response.status_code}, body={response.text}")
        payload = response.json()
        rows.extend((payload.get("bars") or {}).get(symbol, []))
        page_token = payload.get("next_page_token")
        if not page_token:
            return rows
        params["page_token"] = page_token


def upload_raw_bars_to_s3(s3, bucket, prefix, data_kind, feed, start, end, page_number, rows_by_symbol):
    from alfaka.storage.raw_s3_archive import upload_raw_page_to_s3

    return upload_raw_page_to_s3(s3, bucket, prefix, data_kind, feed, start, end, page_number, rows_by_symbol)


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
        "channel": channel,
        "symbol": symbol,
        "eventTime": raw_bar.get("t"),
        "receivedAt": received_at,
        "sourceEventId": event_id,
        "raw": raw_bar,
    }
    candle = normalize_bar(envelope)
    candle["interval"] = interval
    return candle


def raw_bars_to_processed_candles(symbol, raw_bars, feed="sip", interval="1m"):
    return attach_moving_averages([raw_bar_to_processed_candle(symbol, row, feed=feed, interval=interval) for row in raw_bars])


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
