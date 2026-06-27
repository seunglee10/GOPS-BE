# Alfaka Market Data Pipeline

Alpaca SIP 실시간/과거 데이터를 받아 차트 엔진이 사용할 수 있는 Kafka, Redis, S3, ClickHouse 데이터 흐름으로 정리하는 시장 데이터 파이프라인입니다.

기준 명세는 [docs/market-data-pipeline-spec.md](/Users/heejunkim/Desktop/alfaka/docs/market-data-pipeline-spec.md)입니다.

GOPS `Helix` 코드는 이 repo 안에 실제 병합되어 있습니다. 병합 기준은 [README-GOPS-MERGE.md](/Users/heejunkim/Desktop/alfaka/README-GOPS-MERGE.md)와 [docs/gops-helix-merge-report.md](/Users/heejunkim/Desktop/alfaka/docs/gops-helix-merge-report.md)입니다.

담당자별 README:

- [김희준 README](/Users/heejunkim/Desktop/alfaka/README-KIM-HEEJUN.md)
- [조현호 README](/Users/heejunkim/Desktop/alfaka/README-CHO-HYUNHO.md)

## 현재 구조

```text
config/                         Alpaca MVP 구독 채널/심볼 설정
services/01-alpaca-connector/   Alpaca 시세/주문 외부 연동
services/02-kafka-event-publisher/ Kafka 이벤트 발행 계약
services/03-flink-stream-processor/ Kafka Raw -> Processed Kafka + Redis
services/04-redis-state-store/  Redis 캐시/상태 저장 계약
services/05-clickhouse-store/   ClickHouse 저장/조회 계약
services/06-s3-store/           S3 Raw/Processed 저장
services/07-api-websocket/      GOPS API 서버 / WebSocket 서버
apps/gops-frontend/             GOPS React Chart Runtime
apps/chart-engine/              차트 엔진 문서/계약 자리
shared/chart-contract/          GOPS chart command contract
packages/alfaka/                공통 Python 패키지
flink-jobs/                     운영 Flink job 계약/설계 자리
infra/clickhouse/               로컬 ClickHouse schema
infra/docker/                   worker/GOPS 이미지 빌드
infra/k8s/                      AWS/EKS 배포 manifest 초안
infra/aws/                      AWS 접착 설정과 Terraform 초안
scripts/local/                  로컬 검증 스크립트
docs/                           한국어 명세와 코드 가이드
```

임시 화면과 임시 확인 API는 제거했습니다. GOPS 차트 화면은 `apps/gops-frontend`, 과거 조회 API와 실시간 push는 `services/07-api-websocket/gops-backend`에 실제 병합되어 있습니다.

## 데이터 흐름

```text
Alpaca WebSocket
  -> services/01-alpaca-connector
  -> Kafka Raw Topics
  -> services/03-flink-stream-processor 또는 운영 Flink
  -> Kafka Processed Topics
  -> Redis latest/live/series
  -> services/06-s3-store
  -> S3/MinIO
  -> ClickHouse / Chart API / WebSocket Gateway
  -> Chart Engine
```

Raw topic:

```text
market.raw.bars
market.raw.updated-bars
market.raw.trades
```

Processed topic:

```text
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
```

## 로컬 실행

`.env`는 실제 키가 들어가는 로컬 전용 파일입니다. 이 repo에는 예시 `.env`를 만들지 않고, `docker-compose.yml`에서 `env_file: .env`로 붙입니다.

기본 인프라 실행:

```sh
docker compose up -d --build
```

GOPS backend/frontend까지 같이 실행:

```sh
docker compose up -d --build gops-backend gops-frontend
```

접속:

```text
Frontend: http://localhost:5173
Backend:  http://localhost:8000/health
Candles:  http://localhost:8000/api/charts/candles?symbol=AAPL&interval=1m&limit=160
```

Alpaca 실시간 수집기까지 실행:

```sh
docker compose --profile alpaca up -d --build
```

과거 데이터 백필 실행:

```sh
docker compose --profile backfill run --rm historical-backfill
```

검증:

```sh
PYTHONPATH=packages python scripts/local/preview-subscription.py AAPL
PYTHONPATH=packages python scripts/local/send-sample-market-data.py MSFT
PYTHONPATH=packages python scripts/local/check-redis.py MSFT --interval 1m
PYTHONPATH=packages python scripts/local/check-s3.py MSFT --interval 1m
scripts/local/check-clickhouse.sh
```

ClickHouse loader는 기본 `docker compose up -d --build`에 포함됩니다. 이 컨테이너가 `market.ticks.v1`, `market.candles.closed.v1`을 읽어 `market_data.trade_ticks`, `market_data.chart_candles`에 적재합니다.

MinIO Console:

```text
http://localhost:9001
ID/PW: minioadmin / minioadmin
```

ClickHouse:

```text
HTTP: http://localhost:8123
Native: localhost:9002
DB/User/PW: market_data / alfaka / alfaka
```

## AWS에 붙이는 순서

```text
1. infra/aws/terraform/terraform.tfvars.example을 기준으로 terraform.tfvars 작성
2. terraform apply로 worker ECR, S3, Secrets Manager, IRSA 생성
3. scripts/aws/login-ecr.sh 실행
4. scripts/aws/build-and-push-worker.sh 실행
5. infra/k8s/overlays/aws의 placeholder를 실제 AWS 값으로 교체
6. scripts/aws/apply-k8s-aws.sh 실행
7. Flink는 flink-jobs/market-data-normalizer/aws 계약 파일 기준으로 별도 배포
8. Chart API / WebSocket Gateway / Chart Engine은 placeholder 디렉터리에 추가
```
