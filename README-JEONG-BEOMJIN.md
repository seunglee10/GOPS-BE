# 정범진 README

담당: AWS, EKS, image, network, storage 운영

한 줄 책임:

```text
Docker image -> ECR -> EKS Pod -> MSK/Redis/ClickHouse/S3/Secrets 연결
```

## 수정해도 되는 파일

```text
infra/aws/
infra/docker/
infra/k8s/
scripts/aws/
docker-compose.yml
```

## 공동 수정 파일

```text
config/market-data-request.json
docs/data-contracts.md
requirements.txt
apps/gops-frontend/package.json
apps/gops-frontend/package-lock.json
services/07-api-websocket/gops-backend/requirements.txt
```

공동 수정 기준:

```text
정범진: AWS endpoint, ECR image, resource request/limit, IAM, Secret, ingress
김희준: S3 prefix, Kafka topic, Redis key, ClickHouse table
조현호: frontend/backend image build path, service port, proxy/ingress path
```

## 직접 수정하지 않는 파일

```text
apps/chart-engine/src/
apps/gops-frontend/src/
packages/alfaka/
services/01-alpaca-connector/
services/03-flink-stream-processor/
services/05-clickhouse-store/
services/06-s3-store/
services/07-api-websocket/gops-backend/app/
```

## EKS Pod 기준

현재 AWS overlay에 포함되는 pod:

```text
alfaka-alpaca-ingestor       Alpaca WebSocket -> Kafka Raw
alfaka-s3-sink               Kafka Processed -> S3 final/live
alfaka-clickhouse-loader     Kafka closed candles -> ClickHouse
gops-backend                 Redis/ClickHouse -> REST/WebSocket
gops-frontend                React frontend
```

운영 Flink를 쓰는 경우:

```text
local-stream-processor pod는 적용하지 않는다.
Kafka Raw -> Processed/Redis 처리는 Managed Service for Apache Flink 또는 별도 Flink cluster가 맡는다.
```

## S3 스펙

```text
bucket: alfaka-market-data-{env}
region: ap-northeast-2
public access: block all
encryption: SSE-S3 또는 SSE-KMS
versioning: dev는 optional, prod는 enabled 권장
```

prefix:

```text
market-data/raw/alpaca/
market-data/final/candles/
market-data/live/candles/
market-data/live/trades/
```

lifecycle 초안:

```text
market-data/live/*        7일 보관 후 삭제 또는 compact 완료 후 삭제
market-data/raw/alpaca/*  30-90일 보관
market-data/final/*       장기 보관
```

## ClickHouse 스펙

선택지:

```text
dev: docker-compose clickhouse
aws dev: ClickHouse Cloud 또는 EC2/EKS 단일 노드
prod: ClickHouse Cloud 또는 replicated ClickHouse cluster
```

초기 리소스 기준:

```text
100 symbols, candle 중심: 2 vCPU / 8 GB RAM부터 시작
tick 장기 조회까지 켜는 경우: 4-8 vCPU / 16-32 GB RAM부터 재산정
```

연결 env:

```text
CLICKHOUSE_HTTP_URL
CLICKHOUSE_DATABASE=market_data
CLICKHOUSE_USER=alfaka
CLICKHOUSE_PASSWORD
CLICKHOUSE_LOAD_TRADES=false
```

## Redis 스펙

AWS 기준:

```text
service: ElastiCache Redis 또는 Valkey
mode: dev는 single node, prod는 Multi-AZ 권장
TLS: prod enabled 권장
```

초기 리소스 기준:

```text
dev: cache.t4g.small
staging/prod 초기: cache.t4g.medium 또는 r7g.large
```

주요 key:

```text
price:{symbol}:latest
candle:{symbol}:1m:live
candles:{symbol}:{interval}
active:charts:symbols
active:charts:{symbol}
```

## 배포 순서

```sh
scripts/aws/login-ecr.sh
scripts/aws/build-and-push-images.sh
scripts/aws/apply-k8s-aws.sh
```

수정해야 하는 AWS placeholder:

```text
infra/k8s/overlays/aws/kustomization.yaml
infra/k8s/overlays/aws/configmap-aws-patch.yaml
infra/k8s/overlays/aws/serviceaccount-irsa-aws-patch.yaml
```

비밀값은 ConfigMap에 넣지 않습니다.

```text
Alpaca key: AWS Secrets Manager 또는 alfaka-alpaca-secret
ClickHouse password: alfaka-clickhouse-secret
```
