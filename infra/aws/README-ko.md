# AWS 운영 파일

정범진 담당 AWS/EKS 연결 파일입니다.

## 폴더 역할

```text
infra/aws/terraform/        ECR, S3, Secrets Manager, IRSA 생성 자리
infra/k8s/overlays/aws/     EKS에 적용할 Kustomize overlay
scripts/aws/                ECR login/build/push, MSK topic, kubectl apply
infra/aws/msk/topics.txt    MSK topic 목록
flink-jobs/                 운영 Flink job 계약
```

## 이미지

```text
worker image:   infra/docker/Dockerfile.worker
backend image:  infra/docker/Dockerfile.gops-backend
frontend image: infra/docker/Dockerfile.gops-frontend
```

빌드/푸시:

```sh
scripts/aws/build-and-push-images.sh
```

## AWS 서비스

```text
MSK: Kafka Raw/Processed topics
ElastiCache Redis/Valkey: latest/live/recent cache, Pub/Sub, active chart key
S3: raw/final/live market data
ClickHouse: chart_candles query store
Secrets Manager/Kubernetes Secret: Alpaca key, ClickHouse password
IRSA: S3/Secrets 접근 권한
```

상세 스펙은 `README-JEONG-BEOMJIN.md`와 `docs/data-contracts.md`를 기준으로 봅니다.
