# AWS Runtime Values

이 폴더는 실제 `.env` 파일을 보관하지 않습니다.

런타임 값은 아래 위치에 직접 붙입니다.

```text
Alpaca API key/secret    AWS Secrets Manager
Kafka bootstrap servers  infra/k8s/overlays/aws/configmap-aws-patch.yaml
Redis URL                infra/k8s/overlays/aws/configmap-aws-patch.yaml
S3 bucket/prefix         infra/k8s/overlays/aws/configmap-aws-patch.yaml
Worker image             infra/k8s/overlays/aws/kustomization.yaml
IRSA role ARN            infra/k8s/overlays/aws/serviceaccount-irsa-aws-patch.yaml
```

로컬 개발에서는 repo 밖에 공개하지 않는 `/Users/heejunkim/Documents/alfaka/gops/.env`를
`docker-compose.yml`의 `env_file: .env`로 붙입니다.
