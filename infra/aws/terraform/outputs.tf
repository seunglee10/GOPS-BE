# 역할: Terraform 결과를 Kubernetes overlay와 배포 스크립트에 꽂기 쉽게 출력합니다.
# 사용: 출력값을 infra/k8s/overlays/aws 파일의 PLACEHOLDER와 교체합니다.
# 예: worker_ecr_repository_url 값을 image newName에 넣습니다.
output "aws_account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "worker_ecr_repository_url" {
  value = aws_ecr_repository.worker.repository_url
}

output "s3_bucket_name" {
  value = aws_s3_bucket.market_data.bucket
}

output "alpaca_secret_name" {
  value = aws_secretsmanager_secret.alpaca_api.name
}

output "market_data_irsa_role_arn" {
  value = aws_iam_role.market_data_irsa.arn
}

output "kafka_bootstrap_servers" {
  value = var.msk_bootstrap_servers
}

output "redis_url" {
  value = "redis://${var.redis_endpoint}:6379/0"
}
