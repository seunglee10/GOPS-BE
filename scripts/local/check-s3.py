# 역할: S3/MinIO에 저장된 candle archive 파일 목록을 조회합니다.
# 사용: ClickHouse post-insert/backfill archive를 확인합니다.
# 실행: PYTHONPATH=systems/market-data/shared python scripts/local/check-s3.py MSFT --interval 1m
from alfaka.tools.check_s3 import main


if __name__ == "__main__":
    main()
