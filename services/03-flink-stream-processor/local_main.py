# 역할: 로컬 Docker에서 Flink 역할을 흉내 내는 Python 처리기를 실행합니다.
# 사용: AWS 배포 전 Kafka Raw -> Redis/Processed Kafka 계약을 빠르게 검증합니다.
# 운영: 실제 운영에서는 flink-jobs/market-data-normalizer 또는 관리형 Flink로 대체합니다.
from alfaka.streaming.processor import main


if __name__ == "__main__":
    main()
