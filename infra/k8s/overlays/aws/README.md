# AWS Kubernetes Overlay

이 overlay는 EKS에 적용할 Kustomize 기준입니다.

## 적용 전 바꿀 값

```text
YOUR_ACCOUNT_ID
YOUR_MSK_BOOTSTRAP_SERVERS
YOUR_REDIS_ENDPOINT
YOUR_CLICKHOUSE_ENDPOINT
YOUR_S3_BUCKET
YOUR_IRSA_ROLE_ARN
```

## 적용

```sh
kubectl apply -k infra/k8s/overlays/aws
```

## 포함하는 Pod

```text
alfaka-alpaca-ingestor       Alpaca -> MSK Raw Topic
alfaka-s3-sink               MSK Processed Topic -> S3 final/live
alfaka-clickhouse-loader     MSK closed candles -> ClickHouse
gops-backend                 Redis/ClickHouse -> REST/WebSocket
gops-frontend                React frontend
```

## 운영에서 별도 처리

```text
local-stream-processor는 로컬 대체 worker입니다.
운영에서는 Managed Service for Apache Flink 또는 Flink on EKS가 Raw -> Processed/Redis 처리를 맡습니다.
```
