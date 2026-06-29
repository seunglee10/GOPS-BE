#!/usr/bin/env bash
# Create GOPS ECR repositories used by the Kubernetes image overlay.
set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
PROJECT_NAME="${PROJECT_NAME:-alfaka}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
NAME_PREFIX="${PROJECT_NAME}-${ENVIRONMENT}"

repositories=(
  gops-frontend
  gops-api-server
  gops-market-ingestor
  gops-market-processor
  gops-market-storage
  gops-backfill-worker
  gops-order-worker
  gops-kis-adapter
)

for repository in "${repositories[@]}"; do
  name="${NAME_PREFIX}-${repository}"
  if aws ecr describe-repositories --region "${AWS_REGION}" --repository-names "${name}" >/dev/null 2>&1; then
    echo "exists: ${name}"
    continue
  fi

  aws ecr create-repository \
    --region "${AWS_REGION}" \
    --repository-name "${name}" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE \
    --tags Key=Project,Value="${PROJECT_NAME}" Key=Environment,Value="${ENVIRONMENT}" Key=ManagedBy,Value=script
  echo "created: ${name}"
done
