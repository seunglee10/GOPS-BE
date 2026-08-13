# 역할: Kafka Processed 데이터를 ClickHouse에 적재하는 서비스를 실행합니다.
# 사용: GOPS API Server가 ClickHouse에서 과거 캔들을 조회하기 전에 켜야 합니다.
# 설정: CLICKHOUSE_HTTP_URL, KAFKA_CLICKHOUSE_TOPICS, KAFKA_BOOTSTRAP_SERVERS를 사용합니다.
from market_data.storage.clickhouse_loader import main


if __name__ == "__main__":
    main()
