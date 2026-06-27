# 역할: Alpaca Historical REST API에서 과거 데이터를 받아 S3/MinIO에 저장합니다.
# 사용: Redis에 없는 이전 구간을 백필하거나 AWS 적재 전 로컬 MinIO로 확인합니다.
# 출력: S3_PREFIX 아래 JSONL 파일.
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import requests

from alfaka.common.env import load_dotenv, parse_csv, utc_now_iso
from alfaka.common.s3_client import create_s3_client
from alfaka.common.secrets import load_alpaca_credentials


def main():
    load_dotenv()
    alpaca_key, alpaca_secret = load_alpaca_credentials()

    data_kind = os.getenv("HISTORICAL_DATA_KIND", "trades")
    symbols = parse_csv(os.getenv("HISTORICAL_SYMBOLS", os.getenv("ALPACA_SYMBOLS", "")))
    start = os.getenv("HISTORICAL_START")
    end = os.getenv("HISTORICAL_END")
    feed = os.getenv("HISTORICAL_FEED", os.getenv("ALPACA_FEED", "sip"))
    timeframe = os.getenv("HISTORICAL_TIMEFRAME", "1Min")
    limit = os.getenv("HISTORICAL_LIMIT", "10000")

    s3_bucket = os.getenv("S3_BUCKET")
    s3_prefix = os.getenv("S3_PREFIX", "market-data/raw")

    if not alpaca_key or not alpaca_secret:
        print("Alpaca 키가 없습니다. .env 또는 AWS Secrets Manager 설정을 넣어주세요.", file=sys.stderr)
        sys.exit(1)
    if data_kind not in {"trades", "bars"}:
        print("HISTORICAL_DATA_KIND는 trades 또는 bars만 가능합니다.", file=sys.stderr)
        sys.exit(1)
    if not symbols or not start or not end or not s3_bucket:
        print("HISTORICAL_SYMBOLS/START/END와 S3_BUCKET을 설정해주세요.", file=sys.stderr)
        sys.exit(1)

    s3 = create_s3_client()
    endpoint = f"https://data.alpaca.markets/v2/stocks/{data_kind}"
    headers = {"APCA-API-KEY-ID": alpaca_key, "APCA-API-SECRET-KEY": alpaca_secret}
    params = {"symbols": ",".join(symbols), "start": start, "end": end, "feed": feed, "limit": limit, "sort": "asc"}
    if data_kind == "bars":
        params["timeframe"] = timeframe

    print(f"Alpaca 과거 데이터 요청: kind={data_kind}, symbols={symbols}, start={start}, end={end}", flush=True)
    print(f"S3 저장 위치: s3://{s3_bucket}/{s3_prefix}", flush=True)

    page_number = 1
    page_token = None
    while True:
        if page_token:
            params["page_token"] = page_token
        else:
            params.pop("page_token", None)

        response = requests.get(endpoint, headers=headers, params=params, timeout=30)
        if response.status_code >= 400:
            print(f"Alpaca 요청 실패: status={response.status_code}, body={response.text}", file=sys.stderr)
            sys.exit(1)

        payload = response.json()
        rows_by_symbol = payload.get(data_kind, {})
        total_rows = upload_page_to_s3(s3, s3_bucket, s3_prefix, data_kind, feed, start, end, page_number, rows_by_symbol)
        print(f"{page_number}페이지 저장 완료: rows={total_rows}", flush=True)

        page_token = payload.get("next_page_token")
        if not page_token:
            break
        page_number += 1

    print("과거 데이터 S3 저장 완료", flush=True)


def upload_page_to_s3(s3, bucket, prefix, data_kind, feed, start, end, page_number, rows_by_symbol):
    total_rows = 0
    for symbol, rows in rows_by_symbol.items():
        if not rows:
            continue
        rows_by_partition = defaultdict(list)
        for row in rows:
            event_time = row.get("t") or start or end
            partition_key = raw_partition_key(prefix, data_kind, symbol, event_time)
            rows_by_partition[partition_key].append({
                "source": "alpaca",
                "feed": feed,
                "channel": data_kind,
                "symbol": symbol,
                "eventTime": event_time,
                "receivedAt": utc_now_iso(),
                "raw": row,
            })

        for partition_key, partition_rows in rows_by_partition.items():
            body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in partition_rows) + "\n"
            object_key = f"{partition_key}/part-{page_number:06d}.jsonl"
            s3.put_object(Bucket=bucket, Key=object_key, Body=body.encode("utf-8"), ContentType="application/x-ndjson")
            total_rows += len(partition_rows)
            print(f"S3 업로드: s3://{bucket}/{object_key} rows={len(partition_rows)}", flush=True)
    return total_rows


def raw_partition_key(prefix, channel, symbol, event_time):
    parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    return f"{prefix}/source=alpaca/channel={channel}/symbol={symbol}/year={parsed:%Y}/month={parsed:%m}/day={parsed:%d}/hour={parsed:%H}"
