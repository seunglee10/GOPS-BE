# AWS Terraform Foundation

이 폴더는 AWS에 바로 붙기 위한 최소 접착 리소스를 만듭니다.

## 만드는 것

```text
ECR repositories for GOPS custom images
S3 market data bucket reference
Secrets Manager Alpaca secret reference
IRSA IAM role/policy
```

## 만들지 않는 것

```text
EKS cluster
MSK cluster
Flink cluster/application
ElastiCache Redis
VPC/Subnet/NAT
```

위 항목은 회사 공통 인프라 또는 별도 Terraform 모듈에서 만드는 쪽이 안전합니다. 이 폴더는 그 결과값을 받아 애플리케이션을 딱 붙이는 역할입니다.

## 적용 순서

```sh
cd infra/aws/terraform
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

적용 후 `terraform output` 값을 `infra/k8s/overlays/aws`의 placeholder에 넣습니다.

Target custom image repositories:

```text
gops-frontend
gops-api-server
gops-market-ingestor
gops-market-processor
gops-market-storage
gops-backfill-worker
gops-order-worker
gops-kis-adapter
```

Build script variable mapping:

| Terraform output | `scripts/aws/build-and-push-images.sh` env |
| --- | --- |
| `frontend_ecr_repository_url` | `ECR_FRONTEND_REPO` |
| `api_server_ecr_repository_url` | `ECR_API_SERVER_REPO` |
| `market_ingestor_ecr_repository_url` | `ECR_MARKET_INGESTOR_REPO` |
| `market_processor_ecr_repository_url` | `ECR_MARKET_PROCESSOR_REPO` |
| `market_storage_ecr_repository_url` | `ECR_MARKET_STORAGE_REPO` |
| `backfill_worker_ecr_repository_url` | `ECR_BACKFILL_WORKER_REPO` |
| `order_worker_ecr_repository_url` | `ECR_ORDER_WORKER_REPO` |
| `kis_adapter_ecr_repository_url` | `ECR_KIS_ADAPTER_REPO` |

이미지를 전부 다시 빌드하지 않고 변경된 서비스만 빌드/푸시할 수 있습니다.

```sh
AWS_ACCOUNT_ID=<aws-account-id> scripts/aws/build-and-push-images.sh frontend
AWS_ACCOUNT_ID=<aws-account-id> scripts/aws/build-and-push-images.sh backend market-storage
AWS_ACCOUNT_ID=<aws-account-id> SERVICES=frontend,backend scripts/aws/build-and-push-images.sh
```

서비스 이름은 `scripts/aws/build-and-push-images.sh --help`로 확인합니다.

## Secrets Manager

기본값은 이미 만들어진 `dev/alpaca` secret을 참조합니다.

```hcl
alpaca_secret_name   = "dev/alpaca"
create_alpaca_secret = false
```

secret 값은 아래 JSON key 중 하나의 형태여야 합니다.

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

기존 secret이 없고 Terraform으로 빈 secret shell을 만들고 싶을 때만
`create_alpaca_secret = true`로 바꿉니다.

`google_oauth_secret_name`을 비워두면 IRSA 정책에 Google OAuth secret을
추가하지 않습니다. Google login secret을 Secrets Manager에서 읽을 때는
아래처럼 값을 넣으면 gops-backend가 해당 secret을 읽을 수 있도록 같은 pod
policy에 ARN이 포함됩니다.

```hcl
google_oauth_secret_name = "dev/google-oauth"
```

## S3 Bucket

기본값은 이미 만들어진 `gops-market-data-<aws-account-id>-ap-northeast-2-an`
bucket을 참조합니다.

```hcl
s3_bucket_name   = "gops-market-data-<aws-account-id>-ap-northeast-2-an"
create_s3_bucket = false
```

bucket을 Terraform으로 새로 만들 때만 `create_s3_bucket = true`로 바꿉니다.
