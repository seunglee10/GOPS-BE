import json
import os
from datetime import timedelta

from alfaka.common.env import load_dotenv, utc_now_iso
from alfaka.common.market_messages import source_event_id
from alfaka.backfill.status import parse_time, to_iso
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
        interval = record["interval"]
        start = record["range"]["start"]
        end = record["range"]["end"]
        mode = record.get("mode") or os.getenv("BACKFILL_EXECUTION_MODE", "queue")
        feed = os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip"))

        if interval != "1m":
            raise BackfillUnavailable("Backfill v1 supports 1m historical bars.")

        raw_bars = build_sample_raw_bars(symbol, start, end) if mode == "sample-dev" else fetch_alpaca_bars(symbol, start, end, feed)
        if not raw_bars:
            raise BackfillUnavailable("Historical provider returned no bars.")

        raw_prefix = os.getenv("S3_RAW_PREFIX", os.getenv("S3_PREFIX", "market-data/raw/alpaca"))
        final_prefix = os.getenv("S3_FINAL_PREFIX", os.getenv("S3_PROCESSED_PREFIX", "market-data/final"))
        output_format = os.getenv("S3_PROCESSED_FORMAT", "jsonl").lower()

        raw_count = upload_raw_bars_to_s3(self.s3, bucket, raw_prefix, "bars", feed, start, end, 1, {symbol: raw_bars})
        processed = raw_bars_to_processed_candles(symbol, raw_bars, feed=feed)
        partition_key = f"{final_prefix}/candles/interval={interval}/symbol={symbol}/backfill_request={record['requestId'].replace(':', '_')}"
        processed_key = flush_buffer(self.s3, bucket, partition_key, processed, output_format)
        materialized = materialize_s3_processed_objects(self.clickhouse_client, self.s3, bucket, [processed_key], source_name="backfill-worker")

        return {
            "rawRowCount": raw_count,
            "processedRowCount": len(processed),
            "materializedRowCount": materialized["rowCount"],
            "processedObjects": [f"s3://{bucket}/{processed_key}"],
        }


def fetch_alpaca_bars(symbol, start, end, feed):
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
        "timeframe": os.getenv("HISTORICAL_TIMEFRAME", "1Min"),
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


def build_sample_raw_bars(symbol, start, end, count=12):
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    total_seconds = max(60, int((end_dt - start_dt).total_seconds()))
    step_seconds = max(60, total_seconds // max(1, count))
    rows = []
    base = 100 + (sum(ord(char) for char in symbol) % 40)
    for index in range(count):
        timestamp = start_dt + timedelta(seconds=step_seconds * index)
        if timestamp > end_dt:
            break
        open_price = round(base + index * 0.25, 4)
        close_price = round(open_price + 0.12, 4)
        rows.append({
            "t": to_iso(timestamp),
            "o": open_price,
            "h": round(close_price + 0.18, 4),
            "l": round(open_price - 0.14, 4),
            "c": close_price,
            "v": 1000 + index * 17,
            "n": 10 + index,
            "vw": round((open_price + close_price) / 2, 4),
        })
    return rows


def upload_raw_bars_to_s3(s3, bucket, prefix, data_kind, feed, start, end, page_number, rows_by_symbol):
    from alfaka.storage.raw_s3_archive import upload_raw_page_to_s3

    return upload_raw_page_to_s3(s3, bucket, prefix, data_kind, feed, start, end, page_number, rows_by_symbol)


def raw_bar_to_processed_candle(symbol, raw_bar, feed="sip", received_at=None):
    received_at = received_at or utc_now_iso()
    message = {"T": "b", "S": symbol, **raw_bar}
    event_id = source_event_id(message, feed, "bars", symbol, received_at)
    envelope = {
        "source": "alpaca",
        "feed": feed,
        "channel": "bars",
        "symbol": symbol,
        "eventTime": raw_bar.get("t"),
        "receivedAt": received_at,
        "sourceEventId": event_id,
        "raw": raw_bar,
    }
    candle = normalize_bar(envelope)
    candle["source"] = "alpaca.bars"
    return candle


def raw_bars_to_processed_candles(symbol, raw_bars, feed="sip"):
    return attach_moving_averages([raw_bar_to_processed_candle(symbol, row, feed=feed) for row in raw_bars])


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
