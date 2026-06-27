# 역할: AWS Terraform provider와 버전 조건을 고정합니다.
# 사용: terraform init 시 provider를 안정적으로 받습니다.
# 주의: 실제 AWS 계정에 적용하기 전 backend 설정을 먼저 정합니다.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
