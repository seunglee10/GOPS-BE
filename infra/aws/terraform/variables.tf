# 역할: AWS에 붙일 때 바꿔야 하는 값을 한곳에 모읍니다.
# 사용: terraform.tfvars 또는 CI 변수로 채웁니다.
# 출력: Kubernetes ConfigMap/IRSA/ECR/S3에 필요한 값으로 이어집니다.
variable "project_name" {
  type    = string
  default = "alfaka"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "s3_bucket_name" {
  type        = string
  description = "시장 데이터 장기 저장 S3 버킷 이름입니다. 전역 고유해야 합니다."
}

variable "create_s3_bucket" {
  type        = bool
  default     = false
  description = "true면 S3 bucket을 만들고, false면 기존 s3_bucket_name bucket을 참조합니다."
}

variable "alpaca_secret_name" {
  type    = string
  default = "dev/alpaca"
}

variable "kis_secret_name" {
  type        = string
  default     = "tead/gops/kis"
  description = "KIS credential secret name read by the broker adapter."
}

variable "google_oauth_secret_name" {
  type        = string
  default     = ""
  description = "Optional Google OAuth/session secret name read by gops-backend."
}

variable "openai_secret_name" {
  type        = string
  default     = "/gops/prod/agent-orchestrator/openai/api-key"
  description = "OpenAI API key secret name read by agent-orchestrator through External Secrets/IRSA."
}

variable "create_alpaca_secret" {
  type        = bool
  default     = false
  description = "true면 Secrets Manager secret shell을 만들고, false면 기존 alpaca_secret_name secret을 참조합니다."
}

variable "eks_oidc_provider_arn" {
  type        = string
  description = "EKS cluster의 OIDC provider ARN입니다. IRSA를 만들 때 씁니다."
}

variable "eks_oidc_provider_url" {
  type        = string
  description = "https:// 없는 EKS OIDC provider URL입니다."
}

variable "kubernetes_namespace" {
  type    = string
  default = "alfaka-market-data"
}

variable "kubernetes_service_account" {
  type    = string
  default = "alfaka-market-data-sa"
}

variable "msk_bootstrap_servers" {
  type        = string
  description = "Amazon MSK bootstrap broker 문자열입니다. ConfigMap에 넣습니다."
}

variable "redis_endpoint" {
  type        = string
  description = "ElastiCache Redis/Valkey endpoint입니다. 포트 제외 host만 넣습니다."
}
