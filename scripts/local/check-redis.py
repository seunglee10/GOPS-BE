# 역할: Redis 최신 시장 데이터 key를 조회합니다.
# 사용: stream-processor가 현재가/캔들을 제대로 썼는지 확인합니다.
# 실행: PYTHONPATH=systems/market-data/shared python scripts/local/check-redis.py MSFT --interval 1m
from alfaka.tools.check_redis import main


if __name__ == "__main__":
    main()
