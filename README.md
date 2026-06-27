# GOPS Alfaka

Alpaca 시장 데이터를 받아 GOPS 차트 화면에 제공하는 통합 저장소입니다.

현재 기준 구조는 아래와 같습니다.

```text
apps/chart-engine/                 조현호 담당 차트 엔진 TypeScript 패키지
apps/gops-frontend/                조현호 담당 React frontend
services/01-alpaca-connector/      김희준 담당 Alpaca 실시간/과거 수집 entrypoint
services/02-kafka-event-publisher/ 김희준 담당 Kafka topic 계약
services/03-flink-stream-processor/김희준 담당 Raw -> Processed/Redis 처리
services/04-redis-state-store/     김희준 담당 Redis key 계약
services/05-clickhouse-store/      김희준 담당 ClickHouse loader entrypoint
services/06-s3-store/              김희준 담당 S3 sink entrypoint
services/07-api-websocket/         조현호 담당 GOPS backend API/WebSocket
packages/alfaka/                   김희준 중심 Python 공통 코드
infra/docker/                      정범진 담당 container image
infra/k8s/                         정범진 담당 EKS manifest
scripts/aws/                       정범진 담당 AWS 배포 스크립트
config/market-data-request.json    공동 수정 market universe/data policy
docs/data-contracts.md             S3/ClickHouse/Redis/Kafka 기준 계약
```

담당자별 README:

- [김희준 README](README-KIM-HEEJUN.md): Alpaca, Kafka, Redis, S3, ClickHouse 데이터 파이프라인
- [조현호 README](README-CHO-HYUNHO.md): frontend, chart engine, backend API/WebSocket
- [정범진 README](README-JEONG-BEOMJIN.md): AWS, EKS, image, S3/ClickHouse/Redis 운영 스펙

## 데이터 정책

```text
전날까지 확정 저장: S3 market-data/final
오늘 초기 차트 로드: 오늘 1분봉 + ClickHouse 최근 과거 캔들
오늘 tick 전체 초기 로드: 하지 않음
차트 진입 이후: 해당 symbol만 trades 실시간 구독
오늘 tick 저장: S3 market-data/live/trades, 장마감 후 compact 대상
ClickHouse 기본 적재: chart_candles 중심, trade_ticks는 옵션
```

## 로컬 실행

```sh
docker compose up -d --build
docker compose up -d --build gops-backend gops-frontend
```

접속:

```text
Frontend: http://localhost:5173
Backend:  http://localhost:8000/health
Candles:  http://localhost:8000/api/charts/candles?symbol=NVDA&interval=1m&limit=160
```

Alpaca 실시간 수집:

```sh
docker compose --profile alpaca up -d --build
```

과거 1분봉 백필:

```sh
docker compose --profile backfill run --rm historical-backfill
```

## AWS/EKS 배포 흐름

```sh
scripts/aws/login-ecr.sh
scripts/aws/build-and-push-images.sh
scripts/aws/apply-k8s-aws.sh
```

EKS overlay 기준 manifest:

```text
infra/k8s/overlays/aws/kustomization.yaml
```

운영에서 Flink를 Managed Service for Apache Flink로 쓰면 `infra/k8s/base/deployment-local-stream-processor.example.yaml`은 적용하지 않습니다.
