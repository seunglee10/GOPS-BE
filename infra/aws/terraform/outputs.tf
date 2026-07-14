# 역할: Terraform 결과를 Kubernetes overlay와 배포 스크립트에 꽂기 쉽게 출력합니다.
# 사용: 출력값을 infra/k8s/overlays/aws 파일의 PLACEHOLDER와 교체합니다.
# 예: market_ingestor_ecr_repository_url 값을 image newName에 넣습니다.
output "aws_account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "frontend_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["frontend"].repository_url
}

output "api_server_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["api_server"].repository_url
}

output "market_ingestor_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["market_ingestor"].repository_url
}

output "market_processor_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["market_processor"].repository_url
}

output "market_storage_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["market_storage"].repository_url
}

output "order_worker_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["order_worker"].repository_url
}

output "kis_adapter_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["kis_adapter"].repository_url
}

output "agent_orchestrator_ecr_repository_url" {
  value = aws_ecr_repository.custom_images["agent_orchestrator"].repository_url
}

output "s3_bucket_name" {
  value = local.market_data_bucket_name
}

output "ai_coach_snapshot_s3_bucket" {
  value = aws_s3_bucket.ai_coach_snapshots.bucket
}

output "ai_coach_worker_irsa_role_arn" {
  value = aws_iam_role.ai_coach_worker_irsa.arn
}

output "alpaca_secret_name" {
  value = local.alpaca_secret_name
}

output "alpaca_secret_arn" {
  value = local.alpaca_secret_arn
}

output "kis_secret_name" {
  value = var.kis_secret_name
}

output "kis_secret_arn" {
  value = local.kis_secret_arn
}

output "google_oauth_secret_name" {
  value = var.google_oauth_secret_name
}

output "google_oauth_secret_arns" {
  value = local.google_oauth_secret_arns
}

output "openai_secret_name" {
  value = var.openai_secret_name
}

output "openai_secret_arn" {
  value = local.openai_secret_arn
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
