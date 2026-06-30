# 역할: 현재 로컬 Docker와 AWS/EKS에서 사용하는 Python stream processor를 실행합니다.
# 사용: Kafka Raw -> Redis/Processed Kafka 계약을 담당하는 market-processor pod entrypoint입니다.
# 운영: future Flink migration 전까지 이 Python processor가 명시적 runtime unit입니다.
from alfaka.streaming.processor import main


if __name__ == "__main__":
    main()
