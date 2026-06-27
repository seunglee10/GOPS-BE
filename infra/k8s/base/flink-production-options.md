# 운영 Flink 배포 선택지

로컬 Docker의 `local-stream-processor`는 실제 Flink가 아닙니다. 운영에서는 아래 둘 중 하나를 선택합니다.

## 선택 A. Amazon Managed Service for Apache Flink

```text
Alpaca Ingestor Pod -> Amazon MSK -> Managed Flink Application -> MSK Processed Topic -> Redis/S3 Sink
```

장점은 JobManager/TaskManager 운영 부담이 작다는 것입니다. AWS에서 Flink 실행 환경을 관리합니다.

## 선택 B. Flink on EKS

```text
Alpaca Ingestor Pod -> Amazon MSK -> Flink JobManager/TaskManager Pods -> MSK Processed Topic -> Redis/S3 Sink
```

장점은 EKS 안에서 세밀하게 제어할 수 있다는 것입니다. 대신 Flink HA, checkpoint, savepoint, autoscaling 운영을 직접 봐야 합니다.

## 현재 repo에서의 대응

| 로컬 Docker | 운영 대응 |
|---|---|
| `local-stream-processor` 컨테이너 | Managed Flink 또는 Flink on EKS |
| `kafka` 컨테이너 | Amazon MSK 또는 별도 Kafka cluster |
| `redis` 컨테이너 | ElastiCache Redis/Valkey |
| `minio` 컨테이너 | Amazon S3 |
