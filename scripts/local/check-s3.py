# 역할: S3/MinIO에 저장된 final/live 파일 목록을 조회합니다.
# 사용: s3-sink가 candle/trade 데이터를 장기 저장했는지 확인합니다.
# 실행: PYTHONPATH=systems/market-data/shared python scripts/local/check-s3.py MSFT --interval 1m
from alfaka.tools.check_s3 import main


if __name__ == "__main__":
    main()
