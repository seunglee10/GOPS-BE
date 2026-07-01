# 역할: S3/MinIO에 저장된 시장 데이터 archive 파일 목록을 확인합니다.
# 사용: 로컬은 S3_ENDPOINT_URL로 MinIO를 보고, 운영은 AWS S3를 봅니다.
# 출력: symbol/interval 기준 object key 목록.
import argparse
import os
import sys

from alfaka.common.env import load_dotenv
from alfaka.common.s3_client import create_s3_client
from alfaka.storage.s3_prefixes import default_s3_archive_prefix, first_configured_prefix


def print_objects(s3, bucket, prefix):
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=20)
    contents = response.get("Contents", [])
    print(f"s3://{bucket}/{prefix}")
    if not contents:
        print("  파일 없음")
        return
    for item in contents:
        print(f"  {item['Key']}  size={item['Size']}")


def main():
    parser = argparse.ArgumentParser(description="S3 시장 데이터 저장 위치를 확인합니다.")
    parser.add_argument("symbol", nargs="?", default="AAPL", help="확인할 심볼입니다. 예: AAPL")
    parser.add_argument("--interval", default="1m", choices=["1m", "5m", "10m", "1D", "1W", "1M"], help="Candle 주기입니다.")
    args = parser.parse_args()

    load_dotenv()
    bucket = os.getenv("S3_BUCKET")
    final_prefix = first_configured_prefix(["S3_FINAL_PREFIX"], default_s3_archive_prefix("final"))
    if not bucket:
        print("S3_BUCKET을 .env에 넣어주세요.", file=sys.stderr)
        sys.exit(1)

    symbol = args.symbol.upper()
    s3 = create_s3_client()
    print_objects(s3, bucket, f"{final_prefix}/candles/interval={args.interval}/symbol={symbol}/")


if __name__ == "__main__":
    main()
