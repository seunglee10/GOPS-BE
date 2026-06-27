# AWS Kubernetes Overlay

이 overlay는 로컬 Docker가 아니라 EKS에 붙일 manifest입니다.

## 적용 전 바꿀 값

```text
YOUR_ACCOUNT_ID
YOUR_MSK_BOOTSTRAP_SERVERS
YOUR_REDIS_ENDPOINT
YOUR_S3_BUCKET
YOUR_IRSA_ROLE_ARN
```

## 적용

```sh
kubectl apply -k infra/k8s/overlays/aws
```

## 포함하는 서비스

```text
alpaca-ingestor: Alpaca -> MSK Raw Topic
s3-sink: MSK Processed Topic -> S3
```

## 포함하지 않는 것

```text
local-stream-processor
chart-api
websocket-gateway
chart-engine
```

운영 처리 엔진은 `flink-jobs/market-data-normalizer`를 기준으로 Managed Flink 또는 Flink on EKS에 따로 올립니다. Chart API, WebSocket Gateway, Chart Engine은 placeholder 디렉터리에 실제 코드가 들어온 뒤 별도 manifest로 추가합니다.
