#!/usr/bin/env bash
# 역할: GOPS Dockerfile 목록에 필요한 ECR repository를 없으면 생성합니다.
# 규칙: infra/docker/Dockerfile.gops-foo -> alfaka-dev-gops-foo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
PROJECT_NAME="${PROJECT_NAME:-alfaka}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
NAME_PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"

created_count=0
existing_count=0

while IFS=$'\t' read -r _key repository env_var dockerfile; do
  repository_name="${NAME_PREFIX}-${repository}"
  if [[ -n "${!env_var:-}" ]]; then
    repository_name="${!env_var##*/}"
  fi

  if aws ecr describe-repositories --region "${AWS_REGION}" --repository-names "${repository_name}" >/dev/null 2>&1; then
    echo "exists: ${repository_name} (${dockerfile})"
    existing_count=$((existing_count + 1))
    continue
  fi

  aws ecr create-repository \
    --region "${AWS_REGION}" \
    --repository-name "${repository_name}" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE \
    --tags Key=Project,Value="${PROJECT_NAME}" Key=Environment,Value="${ENVIRONMENT}" Key=ManagedBy,Value=github-actions
  echo "created: ${repository_name} (${dockerfile})"
  created_count=$((created_count + 1))
done < <(gops_image_entries)

echo "ECR repositories ready: ${existing_count} existing, ${created_count} created"
