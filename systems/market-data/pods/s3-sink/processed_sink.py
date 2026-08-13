# 역할: Kafka Processed 데이터를 S3/MinIO에 저장합니다.
# 사용: 로컬 검증과 AWS S3 장기 저장 경로를 같은 코드로 확인합니다.
# 설정: KAFKA_PROCESSED_TOPICS, S3_BUCKET, S3_FINAL_PREFIX가 필요합니다.
from market_data.storage.processed_s3_sink import main


if __name__ == "__main__":
    main()
