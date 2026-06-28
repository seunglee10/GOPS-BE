# 역할: AWS에 바로 붙는 공통 리소스 ECR/S3/Secret/IRSA를 준비합니다.
# 사용: EKS, MSK, Redis가 준비된 뒤 이 foundation을 적용합니다.
# 주의: MSK/Flink/Redis 서버 자체 생성은 별도 네트워크/운영 모듈에서 다룹니다.
data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project_name}-${var.environment}"
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
  custom_image_repositories = {
    frontend         = "gops-frontend"
    api_server       = "gops-api-server"
    market_ingestor  = "gops-market-ingestor"
    market_processor = "gops-market-processor"
    market_storage   = "gops-market-storage"
    backfill_worker  = "gops-backfill-worker"
    order_worker     = "gops-order-worker"
    kis_adapter      = "gops-kis-adapter"
  }
}

resource "aws_ecr_repository" "custom_images" {
  for_each             = local.custom_image_repositories
  name                 = "${local.name_prefix}-${each.value}"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(local.common_tags, {
    Image = each.value
  })
}

data "aws_s3_bucket" "market_data" {
  count  = var.create_s3_bucket ? 0 : 1
  bucket = var.s3_bucket_name
}

resource "aws_s3_bucket" "market_data" {
  count  = var.create_s3_bucket ? 1 : 0
  bucket = var.s3_bucket_name
  tags   = local.common_tags
}

resource "aws_s3_bucket_versioning" "market_data" {
  count  = var.create_s3_bucket ? 1 : 0
  bucket = aws_s3_bucket.market_data[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "market_data" {
  count  = var.create_s3_bucket ? 1 : 0
  bucket = aws_s3_bucket.market_data[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_secretsmanager_secret" "alpaca_api" {
  count = var.create_alpaca_secret ? 0 : 1
  name  = var.alpaca_secret_name
}

resource "aws_secretsmanager_secret" "alpaca_api" {
  count       = var.create_alpaca_secret ? 1 : 0
  name        = var.alpaca_secret_name
  description = "Alpaca Market Data API key for ${local.name_prefix}"
  tags        = local.common_tags
}

locals {
  market_data_bucket_name = one(concat(
    aws_s3_bucket.market_data[*].bucket,
    data.aws_s3_bucket.market_data[*].bucket
  ))
  market_data_bucket_arn = one(concat(
    aws_s3_bucket.market_data[*].arn,
    data.aws_s3_bucket.market_data[*].arn
  ))
  alpaca_secret_arn = one(concat(
    aws_secretsmanager_secret.alpaca_api[*].arn,
    data.aws_secretsmanager_secret.alpaca_api[*].arn
  ))
  alpaca_secret_name = one(concat(
    aws_secretsmanager_secret.alpaca_api[*].name,
    data.aws_secretsmanager_secret.alpaca_api[*].name
  ))
}

resource "aws_iam_policy" "market_data_pod_policy" {
  name        = "${local.name_prefix}-market-data-pod-policy"
  description = "Allow EKS pods to read Alpaca secret and write market data to S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = local.alpaca_secret_arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          local.market_data_bucket_arn,
          "${local.market_data_bucket_arn}/*"
        ]
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role" "market_data_irsa" {
  name = "${local.name_prefix}-market-data-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = var.eks_oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${var.eks_oidc_provider_url}:sub" = "system:serviceaccount:${var.kubernetes_namespace}:${var.kubernetes_service_account}",
            "${var.eks_oidc_provider_url}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "market_data_irsa" {
  role       = aws_iam_role.market_data_irsa.name
  policy_arn = aws_iam_policy.market_data_pod_policy.arn
}
