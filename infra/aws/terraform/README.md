# AWS Terraform Foundation

이 폴더는 AWS에 바로 붙기 위한 최소 접착 리소스를 만듭니다.

## 만드는 것

```text
ECR worker repository
S3 market data bucket
Secrets Manager secret shell
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
