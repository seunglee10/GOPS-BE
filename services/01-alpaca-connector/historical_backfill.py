# 역할: Alpaca 과거 데이터를 S3/MinIO에 백필합니다.
# 사용: Redis/ClickHouse에 없는 이전 구간을 S3 Raw 영역에 채우는 일회성 작업입니다.
# 설정: HISTORICAL_* 값과 Alpaca API 키, S3_BUCKET이 필요합니다.
from alfaka.alpaca.historical_backfill import main


if __name__ == "__main__":
    main()
