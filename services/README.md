# Services as Pod Units

`services/`는 운영에서 Pod, Job, 또는 별도 서버로 나뉘는 실행 단위를 기준으로 정리합니다.

```text
01-alpaca-connector/       Alpaca 실시간/과거 데이터 수집 entrypoint
02-kafka-event-publisher/  Kafka topic 계약
03-flink-stream-processor/ Raw -> Processed/Redis 로컬 대체 processor
04-redis-state-store/      Redis key 계약
05-clickhouse-store/       ClickHouse loader entrypoint
06-s3-store/               S3 sink entrypoint
07-api-websocket/          GOPS backend REST/WebSocket
```

공통 Python 로직은 `packages/alfaka/`에 둡니다. `services/*`는 실행 entrypoint와 운영 경계 문서만 둡니다.

운영 EKS pod 기준은 `infra/k8s/overlays/aws/README.md`를 봅니다.
