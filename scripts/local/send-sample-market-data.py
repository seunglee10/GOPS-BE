# 역할: 로컬 검증용 샘플 시장 데이터를 Kafka Raw Topic에 넣습니다.
# 사용: 실제 Alpaca 결제 전에도 Redis/S3 흐름을 테스트할 수 있습니다.
# 실행: PYTHONPATH=packages python scripts/local/send-sample-market-data.py MSFT
from alfaka.tools.send_sample_market_data import main


if __name__ == "__main__":
    main()
