# AWS Terraform Foundation

이 폴더는 AWS에 바로 붙기 위한 최소 접착 리소스를 만듭니다.

## 만드는 것

```text
ECR worker/backend/frontend repositories
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

## S3 Bucket

기본값은 이미 만들어진 `gops-market-data-<aws-account-id>-ap-northeast-2-an`
bucket을 참조합니다.

```hcl
s3_bucket_name   = "gops-market-data-<aws-account-id>-ap-northeast-2-an"
create_s3_bucket = false
```

bucket을 Terraform으로 새로 만들 때만 `create_s3_bucket = true`로 바꿉니다.
