# Services as Pod Units

`services/`는 운영에서 Pod, Job, 또는 별도 서버로 나뉘는 실행 단위를 기준으로 정리합니다.

```text
01-alpaca-connector/       Alpaca 시세/주문 연동
02-kafka-event-publisher/  Kafka 이벤트 발행 계약
03-flink-stream-processor/ Flink 스트림 처리, 로컬 Python 대체 실행기
04-redis-state-store/      Redis 캐시/상태 저장 계약
05-clickhouse-store/       ClickHouse 저장/조회 계약
06-s3-store/               S3 Raw/Processed 저장
07-api-websocket/          API 서버 / WebSocket 서버
```

공통 비즈니스 로직은 `packages/alfaka/`에 둡니다. 각 service 디렉터리는 실행 entrypoint와 운영 경계 문서만 가집니다.

GOPS 병합 시 `services/07-api-websocket/`의 예시는 실제 서버가 아니라 조현호 `backend/app/routes/`와 `backend/app/services/market_data/`로 옮겨질 adapter 계약입니다.
